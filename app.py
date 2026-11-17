"""
Servidor web para Render (plan gratis) + comandos de Telegram.

- /run?key=RUN_KEY   -> lanza la búsqueda (lo usa cron-job.org cada día)
- /telegram          -> recibe los mensajes del bot (webhook de Telegram)

Comandos en Telegram:
    /buscar    lanza una búsqueda ahora
    /estado    última búsqueda y errores
    /terminos  cargos que se están buscando
    /ayuda     lista de comandos

Variables de entorno en Render:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, RUN_KEY (y opcional LINKEDIN_ANTIGUEDAD)
Render entrega RENDER_EXTERNAL_URL automáticamente; con eso el webhook
de Telegram se configura solo al arrancar.
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
_estado = {"corriendo": False, "ultima_ejecucion": None, "ultimo_error": None,
           "origen": None}

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
RUN_KEY = os.getenv("RUN_KEY", "")
# Clave que Telegram envía en cada mensaje para comprobar que viene de Telegram
WEBHOOK_SECRET = hashlib.sha256(f"{TOKEN}:{RUN_KEY}".encode()).hexdigest()[:32]

AYUDA = (
    "🤖 <b>Bot de ofertas – Minería / Geomensura</b>\n\n"
    "/buscar – busca ofertas ahora\n"
    "/estado – última búsqueda y si hubo errores\n"
    "/terminos – cargos que estoy buscando\n"
    "/ayuda – esta lista"
)


# ----------------------------------------------------------------------------
# Búsqueda en segundo plano
# ----------------------------------------------------------------------------
def _ejecutar():
    try:
        bot.main()
        _estado["ultimo_error"] = None
    except SystemExit as e:  # el script usa sys.exit cuando falta configuración
        if e.code not in (0, None):
            _estado["ultimo_error"] = f"El script terminó con código {e.code}"
    except Exception:
        _estado["ultimo_error"] = traceback.format_exc()[-500:]
    finally:
        _estado["ultima_ejecucion"] = datetime.now(timezone.utc).isoformat()
        _estado["corriendo"] = False
        _lock.release()


def lanzar_busqueda(origen):
    """Devuelve True si se inició, False si ya había una en curso."""
    if not _lock.acquire(blocking=False):
        return False
    _estado["corriendo"] = True
    _estado["origen"] = origen
    threading.Thread(target=_ejecutar, daemon=True).start()
    return True


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def responder(chat_id, texto):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": chat_id, "text": texto, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15)
    except requests.RequestException:
        pass


def hora_chile(iso):
    if not iso:
        return "nunca (desde que el servidor despertó)"
    t = datetime.fromisoformat(iso)
    # Chile continental: UTC-3 en verano, UTC-4 en invierno (aprox. abr–sep)
    offset = -4 if 4 <= t.month <= 8 else -3
    return (t + timedelta(hours=offset)).strftime("%d-%m-%Y %H:%M")


def texto_estado():
    if _estado["corriendo"]:
        return "⏳ Hay una búsqueda en curso. Te llegan las ofertas en unos minutos."
    lineas = [f"🕒 Última búsqueda: {hora_chile(_estado['ultima_ejecucion'])}"]
    if _estado["origen"]:
        lineas.append(f"▶️ Lanzada por: {_estado['origen']}")
    if _estado["ultimo_error"]:
        lineas.append(f"⚠️ Error: <code>{bot.html.escape(_estado['ultimo_error'][-300:])}</code>")
    else:
        lineas.append("✅ Sin errores")
    lineas.append(f"📅 Antigüedad LinkedIn: {bot.LINKEDIN_ANTIGUEDAD}")
    return "\n".join(lineas)


def texto_terminos():
    cargos = "\n".join(f"• {t}" for t in bot.BUSQUEDAS)
    return (f"🔎 <b>Cargos que busco</b>\n{cargos}\n\n"
            f"📍 Ubicación: {bot.UBICACION}\n"
            "🌐 Fuentes: LinkedIn y Computrabajo")


def procesar_comando(chat_id, texto):
    comando = texto.strip().split()[0].split("@")[0].lower() if texto.strip() else ""
    if comando == "/buscar":
        if lanzar_busqueda("comando /buscar"):
            responder(chat_id, "🔎 Buscando ofertas… te llegan en unos 2 minutos.")
        else:
            responder(chat_id, "⏳ Ya hay una búsqueda en curso, espera unos minutos.")
    elif comando == "/estado":
        responder(chat_id, texto_estado())
    elif comando == "/terminos":
        responder(chat_id, texto_terminos())
    else:  # /start, /ayuda, /help o cualquier otro texto
        responder(chat_id, AYUDA)


def configurar_webhook():
    url_base = os.getenv("RENDER_EXTERNAL_URL", "")
    if not (TOKEN and url_base):
        return
    api = f"https://api.telegram.org/bot{TOKEN}"
    try:
        requests.post(f"{api}/setWebhook", data={
            "url": f"{url_base}/telegram",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": '["message"]',
        }, timeout=15)
        requests.post(f"{api}/setMyCommands", json={"commands": [
            {"command": "buscar", "description": "Buscar ofertas ahora"},
            {"command": "estado", "description": "Última búsqueda y errores"},
            {"command": "terminos", "description": "Cargos que se están buscando"},
            {"command": "ayuda", "description": "Lista de comandos"},
        ]}, timeout=15)
    except requests.RequestException:
        pass


# ----------------------------------------------------------------------------
# Rutas web
# ----------------------------------------------------------------------------
@app.get("/")
def inicio():
    return jsonify(servicio="bot-ofertas-mineria", **_estado)


@app.get("/run")
def run():
    if not RUN_KEY or request.args.get("key") != RUN_KEY:
        return jsonify(error="clave inválida"), 403
    if not lanzar_busqueda("horario automático"):
        return jsonify(estado="ya hay una búsqueda en curso"), 409
    return jsonify(estado="búsqueda iniciada"), 202


@app.post("/telegram")
def telegram():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "", 403
    msg = (request.get_json(silent=True) or {}).get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    texto = msg.get("text", "")
    if chat_id and texto:
        if CHAT_ID and chat_id != CHAT_ID:
            responder(chat_id, "Este bot es privado.")
        else:
            procesar_comando(chat_id, texto)
    return "", 200  # siempre 200 para que Telegram no reintente


configurar_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
