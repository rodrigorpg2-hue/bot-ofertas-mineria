"""
Servidor web mínimo para Render (plan gratis).

Render free no ejecuta tareas programadas, así que este servidor expone
una dirección que, al visitarla, lanza la búsqueda de ofertas:

    https://TU-SERVICIO.onrender.com/run?key=TU_CLAVE

Un servicio externo gratuito (cron-job.org) visita esa dirección todos
los días a la hora que elijas.

Variables de entorno en Render:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  -> igual que antes
    RUN_KEY                           -> clave para que nadie más active el bot
"""

import os
import threading
import traceback
from datetime import datetime

from flask import Flask, jsonify, request

import buscador_ofertas_telegram as bot

app = Flask(__name__)
_lock = threading.Lock()
_estado = {"corriendo": False, "ultima_ejecucion": None, "ultimo_error": None}


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
        _estado["ultima_ejecucion"] = datetime.utcnow().isoformat() + "Z"
        _estado["corriendo"] = False
        _lock.release()


@app.get("/")
def inicio():
    return jsonify(servicio="bot-ofertas-mineria", **_estado)


@app.get("/run")
def run():
    clave = os.getenv("RUN_KEY", "")
    if not clave or request.args.get("key") != clave:
        return jsonify(error="clave inválida"), 403
    if not _lock.acquire(blocking=False):
        return jsonify(estado="ya hay una búsqueda en curso"), 409
    _estado["corriendo"] = True
    threading.Thread(target=_ejecutar, daemon=True).start()
    # Responde al tiro; la búsqueda sigue en segundo plano (~2 min)
    return jsonify(estado="búsqueda iniciada"), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
