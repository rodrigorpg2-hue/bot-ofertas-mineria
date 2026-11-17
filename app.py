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
    /ayuda       lista de comandos

Los términos se guardan en un mensaje fijado del chat, así no se pierden
cuando Render (plan gratis) se duerme.

Variables de entorno en Render:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, RUN_KEY (y opcional LINKEDIN_ANTIGUEDAD)
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
_terminos_cache = {"lista": None}

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
    "/ayuda – esta lista\n\n"
    "También puedes usar los botones de abajo 👇"
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
def cargar_terminos(chat_id):
    if _terminos_cache["lista"] is not None:
        return _terminos_cache["lista"]
    lista = None
    fijado = tg("getChat", chat_id=chat_id).get("result", {}).get("pinned_message", {})
    texto = fijado.get("text", "")
    if texto.startswith(MARCA_TERMINOS):
        lista = [l[2:].strip() for l in texto.splitlines() if l.startswith("• ") and l[2:].strip()]
    _terminos_cache["lista"] = lista or list(bot.BUSQUEDAS)
    return _terminos_cache["lista"]


def guardar_terminos(chat_id, lista):
    lista = list(dict.fromkeys(t.strip().lower() for t in lista if t.strip()))[:20]
    _terminos_cache["lista"] = lista
    texto = MARCA_TERMINOS + "\n" + "\n".join(f"• {t}" for t in lista)
    msg = tg("sendMessage", chat_id=chat_id, text=texto,
             disable_notification=True).get("result", {})
    if msg.get("message_id"):
        tg("unpinAllChatMessages", chat_id=chat_id)
        tg("pinChatMessage", chat_id=chat_id, message_id=msg["message_id"],
           disable_notification=True)
    return lista


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
def _ejecutar(chat_id, modo, resumen):
    try:
        bot.TELEGRAM_CHAT_ID = chat_id
        terminos = cargar_terminos(chat_id)
        stats = bot.ejecutar_busqueda(terminos=terminos, modo=modo, resumen=resumen)
        _estado["ultimo_resultado"] = stats
        _estado["ultimo_error"] = None
    except Exception:
        _estado["ultimo_error"] = traceback.format_exc()[-500:]
        responder(chat_id, "⚠️ La búsqueda falló. Usa /estado para ver el detalle.")
    finally:
        _estado["ultima_ejecucion"] = datetime.now(timezone.utc).isoformat()
        _estado["corriendo"] = False
        _lock.release()


def lanzar_busqueda(chat_id, modo, origen, resumen=True):
    if not _lock.acquire(blocking=False):
        return False
    _estado.update(corriendo=True, modo=modo, origen=origen)
    threading.Thread(target=_ejecutar, args=(chat_id, modo, resumen), daemon=True).start()
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


def texto_estado():
    if _estado["corriendo"]:
        return f"⏳ Hay una búsqueda en curso ({_estado['modo']})."
    lineas = [f"🕒 Última búsqueda: {hora_chile(_estado['ultima_ejecucion'])}"]
    if _estado["origen"]:
        lineas.append(f"▶️ Lanzada por: {_estado['origen']}")
    res = _estado["ultimo_resultado"]
    if res:
        for nombre in bot.FUENTES:
            prob = res["problemas"].get(nombre)
            lineas.append(f"• {nombre}: {res['por_fuente'].get(nombre, 0)} ofertas"
                          + (f" ⚠️ {bot.html.escape(prob)}" if prob else ""))
        lineas.append(f"📨 Enviadas: {res['enviadas']}")
    if _estado["ultimo_error"]:
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
        responder(chat_id, texto_estado())
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
    else:
        responder(chat_id, AYUDA, TECLADO)


def procesar_boton(callback):
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    data = callback.get("data", "")
    tg("answerCallbackQuery", callback_query_id=callback.get("id"))
    if not chat_id or (CHAT_ID and chat_id != CHAT_ID):
        return
    if data in ("buscar:nuevas", "buscar:todas"):
        iniciar_desde_chat(chat_id, data.split(":")[1])


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
        {"command": "ayuda", "description": "Lista de comandos"},
    ])


# ----------------------------------------------------------------------------
# Rutas web
# ----------------------------------------------------------------------------
@app.get("/")
def inicio():
    datos = {k: v for k, v in _estado.items() if k != "ultimo_resultado"}
    return jsonify(servicio="bot-ofertas-mineria", **datos)


@app.get("/run")
def run():
    if not RUN_KEY or request.args.get("key") != RUN_KEY:
        return jsonify(error="clave inválida"), 403
    if not CHAT_ID:
        return jsonify(error="falta TELEGRAM_CHAT_ID"), 500
    modo = "todas" if request.args.get("modo") == "todas" else "nuevas"
    # En el horario automático solo avisa si hay ofertas o problemas
    if not lanzar_busqueda(CHAT_ID, modo, "horario automático", resumen="auto"):
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
            chat_id = str(msg.get("chat", {}).get("id", ""))
            texto = msg.get("text", "")
            if chat_id and texto:
                if CHAT_ID and chat_id != CHAT_ID:
                    responder(chat_id, "Este bot es privado.")
                else:
                    procesar_comando(chat_id, texto)
    except Exception:
        _estado["ultimo_error"] = traceback.format_exc()[-500:]
    return "", 200  # siempre 200 para que Telegram no reintente


configurar_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
