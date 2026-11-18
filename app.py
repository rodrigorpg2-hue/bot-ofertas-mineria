"""
Servidor web para Render (plan gratis) + comandos de Telegram.

- /run?key=RUN_KEY          -> busca ofertas nuevas (lo usa cron-job.org cada día)
- /run?key=RUN_KEY&modo=todas -> todas las disponibles en Chile
- /telegram                 -> recibe los mensajes del bot (webhook de Telegram)

Comandos en Telegram:
    /buscar      muestra botones: solo nuevas | todas las disponibles en Chile
    /nuevas      busca solo ofertas nuevas
    /todas       busca todas las ofertas disponibles en Chile
    /terminos    muestra las palabras de búsqueda
    /agregar     agrega términos:   /agregar jefe de turno mina, topógrafo
    /quitar      quita un término:  /quitar geomensor   (o /quitar 3)
    /cambiar     reemplaza todos:   /cambiar término 1, término 2, término 3
    /reiniciar   vuelve a los términos originales
    /estado      última búsqueda y errores
    /miid        muestra el ID de Telegram de quien escribe
    /ayuda       lista de comandos
  Solo administrador (TELEGRAM_CHAT_ID):
    /usuarios    lista de usuarios autorizados
    /autorizar   /autorizar ID nombre
    /bloquear    /bloquear ID

Acceso de otros usuarios: cuando alguien nuevo le escribe al bot, el
administrador recibe un aviso con un botón "Autorizar". Cada usuario tiene
sus propios términos e historial. Con ACCESO_PUBLICO=si cualquiera puede usarlo.

Los términos (y la lista de usuarios) se guardan en un mensaje fijado del
chat, así no se pierden cuando Render (plan gratis) se duerme.

Variables de entorno en Render:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, RUN_KEY
    Opcionales: LINKEDIN_ANTIGUEDAD, ACCESO_PUBLICO, USUARIOS_PERMITIDOS
"""

import hashlib
import os
import threading
import traceback
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request

import buscador_ofertas_telegram as bot

app = Flask(__name__)
_lock = threading.Lock()
_estado = {"corriendo": False, "modo": None, "ultima_ejecucion": None,
           "ultimo_error": None, "origen": None, "ultimo_resultado": None}

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
RUN_KEY = os.getenv("RUN_KEY", "")
API = f"https://api.telegram.org/bot{TOKEN}"
WEBHOOK_SECRET = hashlib.sha256(f"{TOKEN}:{RUN_KEY}".encode()).hexdigest()[:32]

MARCA_TERMINOS = "⚙️ TÉRMINOS DE BÚSQUEDA"
MARCA_USUARIOS = "👥 USUARIOS AUTORIZADOS"
# Caché por chat: {chat_id: {"terminos": [...], "usuarios": {id: nombre}}}
_config_cache = {}

# ACCESO_PUBLICO=si en Render deja usar el bot a cualquiera sin autorización
ACCESO_PUBLICO = os.getenv("ACCESO_PUBLICO", "").strip().lower() in ("si", "sí", "true", "1")
# IDs extra separados por coma (opcional, además de los autorizados desde Telegram)
USUARIOS_ENV = {u.strip() for u in os.getenv("USUARIOS_PERMITIDOS", "").split(",") if u.strip()}

BTN_NUEVAS = "🆕 Buscar nuevas"
BTN_TODAS = "🇨🇱 Buscar todas"
BTN_TERMINOS = "📝 Términos"
BTN_ESTADO = "📊 Estado"
TECLADO = {"keyboard": [[BTN_NUEVAS, BTN_TODAS], [BTN_TERMINOS, BTN_ESTADO]],
           "resize_keyboard": True, "is_persistent": True}
BOTONES_BUSCAR = {"inline_keyboard": [[
    {"text": "🆕 Solo nuevas", "callback_data": "buscar:nuevas"},
    {"text": "🇨🇱 Todas en Chile", "callback_data": "buscar:todas"},
]]}

AYUDA = (
    "🤖 <b>Bot de ofertas laborales</b>\n\n"
    "<b>Buscar</b>\n"
    "/buscar – elige entre solo nuevas o todas\n"
    "/nuevas – ofertas nuevas (que no te he enviado)\n"
    "/todas – todas las ofertas disponibles en Chile\n\n"
    "<b>Palabras de búsqueda</b>\n"
    "/terminos – ver los términos actuales\n"
    "/agregar texto1, texto2 – agregar términos\n"
    "/quitar texto (o número) – quitar un término\n"
    "/cambiar texto1, texto2 – reemplazar todos\n"
    "/reiniciar – volver a los términos originales\n\n"
    "/estado – última búsqueda y errores\n"
    "/miid – ver tu ID de Telegram\n"
    "/ayuda – esta lista\n\n"
    "También puedes usar los botones de abajo 👇"
)

AYUDA_ADMIN = (
    "\n\n<b>Administrador</b>\n"
    "/usuarios – ver usuarios autorizados\n"
    "/autorizar ID nombre – dar acceso a alguien\n"
    "/bloquear ID – quitarle el acceso"
)


# ----------------------------------------------------------------------------
# Telegram: utilidades
# ----------------------------------------------------------------------------
def tg(metodo, **datos):
    try:
        r = requests.post(f"{API}/{metodo}", json=datos, timeout=15)
        return r.json()
    except Exception:
        return {}


def responder(chat_id, texto, teclado=None):
    datos = {"chat_id": chat_id, "text": texto, "parse_mode": "HTML",
             "disable_web_page_preview": True}
    if teclado:
        datos["reply_markup"] = teclado
    return tg("sendMessage", **datos)


# ----------------------------------------------------------------------------
# Términos de búsqueda (persistidos en un mensaje fijado del chat)
# ----------------------------------------------------------------------------
def leer_config(chat_id):
    """Lee la configuración guardada en el mensaje fijado del chat."""
    chat_id = str(chat_id)
    if chat_id in _config_cache:
        return _config_cache[chat_id]
    terminos, usuarios = [], {}
    fijado = tg("getChat", chat_id=chat_id).get("result", {}).get("pinned_message", {})
    texto = fijado.get("text", "")
    if texto.startswith(MARCA_TERMINOS):
        for linea in texto.splitlines():
            if linea.startswith("• ") and linea[2:].strip():
                terminos.append(linea[2:].strip())
            elif linea.startswith("👤 "):
                partes = linea[2:].strip().split(maxsplit=1)
                if partes and partes[0].lstrip("-").isdigit():
                    usuarios[partes[0]] = partes[1] if len(partes) > 1 else ""
    _config_cache[chat_id] = {"terminos": terminos or list(bot.BUSQUEDAS), "usuarios": usuarios}
    return _config_cache[chat_id]


def escribir_config(chat_id, terminos=None, usuarios=None):
    """Guarda términos (y, en el chat del administrador, los usuarios) en un mensaje fijado."""
    chat_id = str(chat_id)
    cfg = leer_config(chat_id)
    if terminos is not None:
        cfg["terminos"] = list(dict.fromkeys(t.strip().lower() for t in terminos if t.strip()))[:20]
    if usuarios is not None:
        cfg["usuarios"] = usuarios
    texto = MARCA_TERMINOS + "\n" + "\n".join(f"• {t}" for t in cfg["terminos"])
    if cfg["usuarios"]:
        texto += f"\n\n{MARCA_USUARIOS}\n" + "\n".join(
            f"👤 {uid} {nombre}".rstrip() for uid, nombre in cfg["usuarios"].items())
    msg = tg("sendMessage", chat_id=chat_id, text=texto,
             disable_notification=True).get("result", {})
    if msg.get("message_id"):
        tg("unpinAllChatMessages", chat_id=chat_id)
        tg("pinChatMessage", chat_id=chat_id, message_id=msg["message_id"],
           disable_notification=True)
    return cfg


def cargar_terminos(chat_id):
    return leer_config(chat_id)["terminos"]


def guardar_terminos(chat_id, lista):
    return escribir_config(chat_id, terminos=lista)["terminos"]


# ----------------------------------------------------------------------------
# Acceso
# ----------------------------------------------------------------------------
def es_admin(chat_id):
    return bool(CHAT_ID) and str(chat_id) == CHAT_ID


def usuarios_autorizados():
    """{id: nombre} de quienes pueden usar el bot (guardado en el chat del administrador)."""
    if not CHAT_ID:
        return {}
    return dict(leer_config(CHAT_ID)["usuarios"])


def tiene_acceso(chat_id):
    chat_id = str(chat_id)
    if not CHAT_ID or ACCESO_PUBLICO or es_admin(chat_id) or chat_id in USUARIOS_ENV:
        return True
    return chat_id in usuarios_autorizados()


def nombre_de(chat):
    nombre = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
    if chat.get("username"):
        nombre += f" (@{chat['username']})"
    return nombre.strip() or "sin nombre"


def solicitar_acceso(chat):
    chat_id = str(chat.get("id"))
    nombre = nombre_de(chat)
    responder(chat_id, "🔒 Este bot necesita autorización.\n\n"
                       f"Tu ID es <code>{chat_id}</code>.\n"
                       "Ya le avisé al administrador; te escribo cuando te dé acceso.")
    if CHAT_ID:
        responder(CHAT_ID,
                  f"👤 <b>{bot.html.escape(nombre)}</b> quiere usar el bot.\n"
                  f"ID: <code>{chat_id}</code>",
                  {"inline_keyboard": [[
                      {"text": "✅ Autorizar", "callback_data": f"autorizar:{chat_id}"},
                      {"text": "❌ Ignorar", "callback_data": f"ignorar:{chat_id}"},
                  ]]})
        _pendientes[chat_id] = nombre


_pendientes = {}  # id -> nombre de quienes pidieron acceso (mientras el servidor está despierto)


def autorizar(uid, nombre=""):
    usuarios = usuarios_autorizados()
    usuarios[str(uid)] = (nombre or _pendientes.get(str(uid), "")).replace("\n", " ")[:60]
    escribir_config(CHAT_ID, usuarios=usuarios)
    _pendientes.pop(str(uid), None)
    responder(str(uid), "✅ ¡Ya tienes acceso al bot!\n\n" + AYUDA, TECLADO)


def bloquear(uid):
    usuarios = usuarios_autorizados()
    if str(uid) not in usuarios:
        return False
    usuarios.pop(str(uid))
    escribir_config(CHAT_ID, usuarios=usuarios)
    return True


def texto_usuarios():
    usuarios = usuarios_autorizados()
    if ACCESO_PUBLICO:
        cab = "🌐 El bot está en modo público: cualquiera puede usarlo.\n\n"
    else:
        cab = ""
    if not usuarios and not USUARIOS_ENV:
        return cab + ("👥 No hay otros usuarios autorizados.\n\n"
                      "Cuando alguien le escriba al bot, te llegará un aviso con un botón para autorizarlo.\n"
                      "También puedes usar <code>/autorizar ID nombre</code>.")
    lineas = [f"👤 <code>{uid}</code> {bot.html.escape(n)}" for uid, n in usuarios.items()]
    lineas += [f"👤 <code>{uid}</code> (desde Render)" for uid in USUARIOS_ENV]
    return cab + "👥 <b>Usuarios autorizados</b>\n" + "\n".join(lineas) + \
        "\n\nPara quitar a alguien: <code>/bloquear ID</code>"


def texto_terminos(lista):
    cargos = "\n".join(f"{i}. {t}" for i, t in enumerate(lista, 1))
    return (f"🔎 <b>Términos de búsqueda</b>\n{cargos}\n\n"
            f"📍 Ubicación: {bot.UBICACION} · 🌐 LinkedIn y Computrabajo\n\n"
            "Para cambiarlos:\n"
            "• <code>/agregar jefe de turno mina, topógrafo</code>\n"
            "• <code>/quitar 2</code>  o  <code>/quitar geomensor</code>\n"
            "• <code>/cambiar término 1, término 2</code>\n"
            "• <code>/reiniciar</code>")


def separar(texto):
    return [t.strip() for t in texto.replace(";", ",").replace("\n", ",").split(",") if t.strip()]


# ----------------------------------------------------------------------------
# Búsqueda en segundo plano
# ----------------------------------------------------------------------------
CARPETA = bot.Path(bot.__file__).parent


def _buscar_para(chat_id, modo, resumen):
    """Ejecuta la búsqueda de un usuario con sus términos y su propio historial."""
    chat_id = str(chat_id)
    bot.TELEGRAM_CHAT_ID = chat_id
    bot.ARCHIVO_HISTORIAL = (CARPETA / "ofertas_enviadas.json" if es_admin(chat_id)
                             else CARPETA / f"historial_{chat_id}.json")
    stats = bot.ejecutar_busqueda(terminos=cargar_terminos(chat_id), modo=modo, resumen=resumen)
    _estado["ultimo_resultado"] = stats
    _ultimo_por_usuario[chat_id] = stats


_ultimo_por_usuario = {}


def _ejecutar(chat_ids, modo, resumen):
    try:
        _estado["ultimo_error"] = None
        for chat_id in chat_ids:
            try:
                _buscar_para(chat_id, modo, resumen)
            except Exception:
                _estado["ultimo_error"] = traceback.format_exc()[-500:]
                responder(chat_id, "⚠️ La búsqueda falló. Usa /estado para ver el detalle.")
    finally:
        _estado["ultima_ejecucion"] = datetime.now(timezone.utc).isoformat()
        _estado["corriendo"] = False
        _lock.release()


def lanzar_busqueda(chat_ids, modo, origen, resumen=True):
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]
    if not _lock.acquire(blocking=False):
        return False
    _estado.update(corriendo=True, modo=modo, origen=origen)
    threading.Thread(target=_ejecutar, args=(list(chat_ids), modo, resumen), daemon=True).start()
    return True


def iniciar_desde_chat(chat_id, modo):
    if lanzar_busqueda(chat_id, modo, f"Telegram ({modo})"):
        if modo == "todas":
            responder(chat_id, "🇨🇱 Buscando <b>todas</b> las ofertas disponibles en Chile…\n"
                               "Puede tardar 5–10 minutos. Te las mando agrupadas de a 10.")
        else:
            responder(chat_id, "🆕 Buscando ofertas <b>nuevas</b>… te aviso en unos 2–3 minutos.")
    else:
        responder(chat_id, "⏳ Ya hay una búsqueda en curso, espera a que termine.")


# ----------------------------------------------------------------------------
# Estado
# ----------------------------------------------------------------------------
def hora_chile(iso):
    if not iso:
        return "ninguna desde que el servidor despertó"
    t = datetime.fromisoformat(iso)
    offset = -4 if 4 <= t.month <= 8 else -3  # Chile: invierno UTC-4, verano UTC-3
    return (t + timedelta(hours=offset)).strftime("%d-%m-%Y %H:%M")


def texto_estado(chat_id):
    if _estado["corriendo"]:
        return f"⏳ Hay una búsqueda en curso ({_estado['modo']})."
    lineas = [f"🕒 Última búsqueda del bot: {hora_chile(_estado['ultima_ejecucion'])}"]
    if _estado["origen"] and es_admin(chat_id):
        lineas.append(f"▶️ Lanzada por: {_estado['origen']}")
    res = _ultimo_por_usuario.get(str(chat_id))
    if res:
        lineas.append("\n<b>Tu última búsqueda</b>")
    if res:
        for nombre in bot.FUENTES:
            prob = res["problemas"].get(nombre)
            lineas.append(f"• {nombre}: {res['por_fuente'].get(nombre, 0)} ofertas"
                          + (f" ⚠️ {bot.html.escape(prob)}" if prob else ""))
        lineas.append(f"📨 Enviadas: {res['enviadas']}")
    if _estado["ultimo_error"] and es_admin(chat_id):
        lineas.append(f"⚠️ Error: <code>{bot.html.escape(_estado['ultimo_error'][-300:])}</code>")
    return "\n".join(lineas)


# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------
ALIAS = {
    BTN_NUEVAS.lower(): "/nuevas", BTN_TODAS.lower(): "/todas",
    BTN_TERMINOS.lower(): "/terminos", BTN_ESTADO.lower(): "/estado",
    "/términos": "/terminos", "/help": "/ayuda", "/start": "/ayuda",
    "/reiniciar_terminos": "/reiniciar",
}


def procesar_comando(chat_id, texto):
    texto = texto.strip()
    if texto.lower() in ALIAS:
        comando, resto = ALIAS[texto.lower()], ""
    else:
        partes = texto.split(maxsplit=1)
        comando = partes[0].split("@")[0].lower()
        resto = partes[1] if len(partes) > 1 else ""
        # Acepta "\buscar" o "buscar" sin barra
        comando = "/" + comando.lstrip("/\\")
        comando = ALIAS.get(comando, comando)

    if comando == "/buscar":
        if resto.lower().startswith("toda"):
            iniciar_desde_chat(chat_id, "todas")
        elif resto.lower().startswith("nueva"):
            iniciar_desde_chat(chat_id, "nuevas")
        else:
            responder(chat_id, "¿Qué ofertas busco?", BOTONES_BUSCAR)
    elif comando == "/nuevas":
        iniciar_desde_chat(chat_id, "nuevas")
    elif comando == "/todas":
        iniciar_desde_chat(chat_id, "todas")
    elif comando == "/estado":
        responder(chat_id, texto_estado(chat_id))
    elif comando == "/terminos":
        responder(chat_id, texto_terminos(cargar_terminos(chat_id)))
    elif comando == "/agregar":
        nuevos = separar(resto)
        if not nuevos:
            responder(chat_id, "Escribe los términos después del comando, separados por coma.\n"
                               "Ej: <code>/agregar jefe de turno mina, topógrafo</code>")
            return
        lista = guardar_terminos(chat_id, cargar_terminos(chat_id) + nuevos)
        responder(chat_id, "✅ Términos agregados.\n\n" + texto_terminos(lista))
    elif comando == "/quitar":
        lista = list(cargar_terminos(chat_id))
        objetivo = resto.strip().lower()
        if objetivo.isdigit() and 1 <= int(objetivo) <= len(lista):
            quitado = lista.pop(int(objetivo) - 1)
        elif objetivo in lista:
            lista.remove(objetivo)
            quitado = objetivo
        else:
            responder(chat_id, "No encontré ese término. Usa el número o el texto exacto de /terminos.")
            return
        if not lista:
            responder(chat_id, "Debe quedar al menos un término.")
            return
        lista = guardar_terminos(chat_id, lista)
        responder(chat_id, f"🗑 Quitado: {bot.html.escape(quitado)}\n\n" + texto_terminos(lista))
    elif comando == "/cambiar":
        nuevos = separar(resto)
        if not nuevos:
            responder(chat_id, "Escribe los nuevos términos separados por coma.\n"
                               "Ej: <code>/cambiar planificador de mantenimiento, ingeniero de confiabilidad</code>")
            return
        lista = guardar_terminos(chat_id, nuevos)
        responder(chat_id, "✅ Términos reemplazados.\n\n" + texto_terminos(lista))
    elif comando == "/reiniciar":
        lista = guardar_terminos(chat_id, list(bot.BUSQUEDAS))
        responder(chat_id, "↩️ Volví a los términos originales.\n\n" + texto_terminos(lista))
    elif comando == "/miid":
        responder(chat_id, f"🆔 Tu ID de Telegram es <code>{chat_id}</code>")
    elif comando in ("/usuarios", "/autorizar", "/bloquear"):
        if not es_admin(chat_id):
            responder(chat_id, "Este comando es solo para el administrador del bot.")
        elif comando == "/usuarios":
            responder(chat_id, texto_usuarios())
        elif comando == "/autorizar":
            partes = resto.split(maxsplit=1)
            if not partes or not partes[0].lstrip("-").isdigit():
                responder(chat_id, "Uso: <code>/autorizar ID nombre</code>\n"
                                   "Ej: <code>/autorizar 123456789 Juan Pérez</code>\n"
                                   "La persona puede ver su ID escribiéndole /miid al bot.")
            else:
                autorizar(partes[0], partes[1] if len(partes) > 1 else "")
                responder(chat_id, f"✅ Usuario <code>{partes[0]}</code> autorizado.\n\n" + texto_usuarios())
        else:  # /bloquear
            uid = resto.strip()
            if bloquear(uid):
                responder(chat_id, f"🚫 Usuario <code>{bot.html.escape(uid)}</code> ya no tiene acceso.")
            else:
                responder(chat_id, "Ese ID no está en la lista. Revisa /usuarios.")
    else:
        responder(chat_id, AYUDA + (AYUDA_ADMIN if es_admin(chat_id) else ""), TECLADO)


def procesar_boton(callback):
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    data = callback.get("data", "")
    tg("answerCallbackQuery", callback_query_id=callback.get("id"))
    if not chat_id or not tiene_acceso(chat_id):
        return
    accion, _, valor = data.partition(":")
    if accion == "buscar" and valor in ("nuevas", "todas"):
        iniciar_desde_chat(chat_id, valor)
    elif accion in ("autorizar", "ignorar") and es_admin(chat_id):
        mensaje = callback.get("message", {})
        if accion == "autorizar":
            nombre = _pendientes.get(valor, "")
            autorizar(valor, nombre)
            texto = f"✅ Autorizaste a {bot.html.escape(nombre or valor)} (<code>{valor}</code>)."
        else:
            _pendientes.pop(valor, None)
            texto = f"❌ Solicitud de <code>{valor}</code> ignorada."
        tg("editMessageText", chat_id=chat_id, message_id=mensaje.get("message_id"),
           text=texto, parse_mode="HTML")


# ----------------------------------------------------------------------------
# Configuración inicial del bot
# ----------------------------------------------------------------------------
def configurar_webhook():
    url_base = os.getenv("RENDER_EXTERNAL_URL", "")
    if not (TOKEN and url_base):
        return
    tg("setWebhook", url=f"{url_base}/telegram", secret_token=WEBHOOK_SECRET,
       allowed_updates=["message", "callback_query"])
    tg("setMyCommands", commands=[
        {"command": "buscar", "description": "Buscar ofertas (nuevas o todas)"},
        {"command": "nuevas", "description": "Solo ofertas nuevas"},
        {"command": "todas", "description": "Todas las ofertas disponibles en Chile"},
        {"command": "terminos", "description": "Ver palabras de búsqueda"},
        {"command": "agregar", "description": "Agregar términos (separados por coma)"},
        {"command": "quitar", "description": "Quitar un término"},
        {"command": "cambiar", "description": "Reemplazar todos los términos"},
        {"command": "reiniciar", "description": "Volver a los términos originales"},
        {"command": "estado", "description": "Última búsqueda y errores"},
        {"command": "miid", "description": "Ver tu ID de Telegram"},
        {"command": "ayuda", "description": "Lista de comandos"},
    ])
    if CHAT_ID:  # el administrador ve además sus comandos
        tg("setMyCommands", scope={"type": "chat", "chat_id": int(CHAT_ID)}, commands=[
            {"command": "buscar", "description": "Buscar ofertas (nuevas o todas)"},
            {"command": "nuevas", "description": "Solo ofertas nuevas"},
            {"command": "todas", "description": "Todas las ofertas disponibles en Chile"},
            {"command": "terminos", "description": "Ver palabras de búsqueda"},
            {"command": "agregar", "description": "Agregar términos (separados por coma)"},
            {"command": "quitar", "description": "Quitar un término"},
            {"command": "cambiar", "description": "Reemplazar todos los términos"},
            {"command": "reiniciar", "description": "Volver a los términos originales"},
            {"command": "estado", "description": "Última búsqueda y errores"},
            {"command": "usuarios", "description": "Usuarios autorizados"},
            {"command": "autorizar", "description": "Dar acceso: /autorizar ID nombre"},
            {"command": "bloquear", "description": "Quitar acceso: /bloquear ID"},
            {"command": "ayuda", "description": "Lista de comandos"},
        ])


# ----------------------------------------------------------------------------
# Rutas web
# ----------------------------------------------------------------------------
@app.get("/")
def inicio():
    datos = {k: v for k, v in _estado.items() if k not in ("ultimo_resultado", "ultimo_error")}
    return jsonify(servicio="bot-ofertas-mineria", **datos)


@app.get("/run")
def run():
    if not RUN_KEY or request.args.get("key") != RUN_KEY:
        return jsonify(error="clave inválida"), 403
    if not CHAT_ID:
        return jsonify(error="falta TELEGRAM_CHAT_ID"), 500
    modo = "todas" if request.args.get("modo") == "todas" else "nuevas"
    destinatarios = [CHAT_ID] + [u for u in list(usuarios_autorizados()) + sorted(USUARIOS_ENV)
                                 if u != CHAT_ID]
    destinatarios = list(dict.fromkeys(destinatarios))
    # En el horario automático solo avisa si hay ofertas o problemas
    if not lanzar_busqueda(destinatarios, modo, "horario automático", resumen="auto"):
        return jsonify(estado="ya hay una búsqueda en curso"), 409
    return jsonify(estado="búsqueda iniciada", modo=modo), 202


@app.post("/telegram")
def telegram():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "", 403
    update = request.get_json(silent=True) or {}
    try:
        if "callback_query" in update:
            procesar_boton(update["callback_query"])
        else:
            msg = update.get("message") or {}
            chat = msg.get("chat", {})
            chat_id = str(chat.get("id", ""))
            texto = msg.get("text", "")
            if chat_id and texto and chat.get("type", "private") == "private":
                if tiene_acceso(chat_id):
                    procesar_comando(chat_id, texto)
                elif texto.strip().lower().lstrip("/\\").startswith("miid"):
                    responder(chat_id, f"🆔 Tu ID de Telegram es <code>{chat_id}</code>")
                elif chat_id in _pendientes:
                    responder(chat_id, "⏳ Tu solicitud está pendiente. Te aviso cuando el administrador te autorice.")
                else:
                    solicitar_acceso(chat)
    except Exception:
        _estado["ultimo_error"] = traceback.format_exc()[-500:]
    return "", 200  # siempre 200 para que Telegram no reintente


configurar_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
