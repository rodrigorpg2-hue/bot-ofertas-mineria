"""
Buscador de ofertas laborales -> Telegram
Perfiles: Ingeniero Civil en Minas / Ingeniero Geomensor (Chile)
Fuentes: LinkedIn (búsqueda pública) y Computrabajo Chile

Requisitos:
    pip install requests beautifulsoup4

Configuración (variables de entorno, o edita los valores por defecto abajo):
    TELEGRAM_TOKEN   -> token del bot (te lo da @BotFather)
    TELEGRAM_CHAT_ID -> tu chat id (ver instrucciones)

Uso:
    python buscador_ofertas_telegram.py            # busca y envía ofertas nuevas
    python buscador_ofertas_telegram.py --test     # solo envía un mensaje de prueba
    python buscador_ofertas_telegram.py --dry-run  # busca y muestra en pantalla, sin enviar
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------
# El token se lee del secreto de GitHub (TELEGRAM_TOKEN). No lo escribas aquí.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
# Si no defines TELEGRAM_CHAT_ID, el script lo detecta solo a partir del
# último mensaje que le hayas enviado al bot.
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Términos de búsqueda (agrega o quita a gusto)
BUSQUEDAS = [
    "ingeniero civil en minas",
    "ingeniero civil de minas",
    "ingeniero en minas",
    "ingeniero geomensor",
    "geomensor",
]

UBICACION = "Chile"

# Antigüedad máxima en LinkedIn: r86400 = 24 h | r604800 = 7 días | r2592000 = 30 días
LINKEDIN_ANTIGUEDAD = os.getenv("LINKEDIN_ANTIGUEDAD", "r604800")
LINKEDIN_PAGINAS = 4          # 25 ofertas por página

COMPUTRABAJO_PAGINAS = 3      # ~20 ofertas por página

# Filtro: el título debe contener al menos una de estas palabras (en minúsculas).
# Deja la lista vacía para no filtrar.
PALABRAS_CLAVE_TITULO = ["minas", "mina", "minero", "minería", "mineria",
                         "geomensor", "geomensura", "topógraf", "topograf"]

# Excluir títulos con estas palabras
PALABRAS_EXCLUIDAS = ["práctica", "practica", "practicante", "alumno en práctica"]

# Archivo donde se guardan las ofertas ya enviadas (para no repetir)
ARCHIVO_HISTORIAL = Path(__file__).with_name("ofertas_enviadas.json")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "es-CL,es;q=0.9",
}

# ----------------------------------------------------------------------------
# UTILIDADES
# ----------------------------------------------------------------------------
def limpiar(texto):
    return re.sub(r"\s+", " ", texto or "").strip()


def cargar_historial():
    if ARCHIVO_HISTORIAL.exists():
        try:
            return set(json.loads(ARCHIVO_HISTORIAL.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def guardar_historial(ids):
    # Guardamos como máximo las últimas 5000 para que el archivo no crezca sin fin
    lista = list(ids)[-5000:]
    ARCHIVO_HISTORIAL.write_text(json.dumps(lista, ensure_ascii=False), encoding="utf-8")


def titulo_valido(titulo):
    t = titulo.lower()
    if any(p in t for p in PALABRAS_EXCLUIDAS):
        return False
    if not PALABRAS_CLAVE_TITULO:
        return True
    return any(p in t for p in PALABRAS_CLAVE_TITULO)


# ----------------------------------------------------------------------------
# FUENTE 1: LINKEDIN (endpoint público de búsqueda, sin login)
# ----------------------------------------------------------------------------
def buscar_linkedin(termino):
    ofertas = []
    for pagina in range(LINKEDIN_PAGINAS):
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
               f"?keywords={quote_plus(termino)}&location={quote_plus(UBICACION)}"
               f"&f_TPR={LINKEDIN_ANTIGUEDAD}&start={pagina * 25}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200 or not r.text.strip():
                break
        except requests.RequestException as e:
            print(f"  [LinkedIn] error de conexión: {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        tarjetas = soup.select("li")
        if not tarjetas:
            break

        for li in tarjetas:
            a = li.select_one("a.base-card__full-link") or li.select_one("a")
            titulo = li.select_one("h3")
            if not a or not titulo:
                continue
            link = a.get("href", "").split("?")[0]
            m = re.search(r"(\d{8,})", link)
            job_id = m.group(1) if m else link
            empresa = li.select_one("h4")
            lugar = li.select_one(".job-search-card__location")
            fecha = li.select_one("time")
            ofertas.append({
                "id": f"li-{job_id}",
                "fuente": "LinkedIn",
                "titulo": limpiar(titulo.get_text()),
                "empresa": limpiar(empresa.get_text()) if empresa else "",
                "lugar": limpiar(lugar.get_text()) if lugar else "",
                "fecha": fecha.get("datetime", "") if fecha else "",
                "link": link,
            })
        time.sleep(1.5)
    return ofertas


# ----------------------------------------------------------------------------
# FUENTE 2: COMPUTRABAJO CHILE
# ----------------------------------------------------------------------------
def buscar_computrabajo(termino):
    ofertas = []
    slug = quote(termino.lower().replace(" ", "-"))
    for pagina in range(1, COMPUTRABAJO_PAGINAS + 1):
        url = f"https://cl.computrabajo.com/trabajo-de-{slug}"
        if pagina > 1:
            url += f"?p={pagina}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                break
        except requests.RequestException as e:
            print(f"  [Computrabajo] error de conexión: {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        tarjetas = soup.select("article.box_offer")
        if not tarjetas:
            break

        for art in tarjetas:
            a = art.select_one("h2 a") or art.select_one("a.js-o-link")
            if not a:
                continue
            link = a.get("href", "")
            if link.startswith("/"):
                link = "https://cl.computrabajo.com" + link
            link = link.split("#")[0]
            job_id = art.get("data-id") or link
            empresa = art.select_one("p a") or art.select_one("p.fs16 a")
            lugar = art.select_one("p span.mr10") or art.select_one("p.fs16 span")
            fecha = art.select_one("p.fs13") or art.select_one("p.fc_aux")
            ofertas.append({
                "id": f"ct-{job_id}",
                "fuente": "Computrabajo",
                "titulo": limpiar(a.get_text()),
                "empresa": limpiar(empresa.get_text()) if empresa else "",
                "lugar": limpiar(lugar.get_text()) if lugar else "",
                "fecha": limpiar(fecha.get_text()) if fecha else "",
                "link": link,
            })
        time.sleep(1.5)
    return ofertas


FUENTES = [buscar_linkedin, buscar_computrabajo]

# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------
def detectar_chat_id():
    """Obtiene el chat id del último mensaje recibido por el bot."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=20)
        for upd in reversed(r.json().get("result", [])):
            msg = upd.get("message") or upd.get("channel_post") or {}
            if msg.get("chat", {}).get("id"):
                return str(msg["chat"]["id"])
    except Exception as ex:
        print(f"  [Telegram] no se pudo detectar el chat id: {ex}")
    return ""


def enviar_telegram(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    datos = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for _ in range(3):
        r = requests.post(url, data=datos, timeout=20)
        if r.status_code == 200:
            return True
        if r.status_code == 429:  # demasiados mensajes: esperar lo que pida Telegram
            espera = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(espera + 1)
            continue
        print(f"  [Telegram] error {r.status_code}: {r.text[:200]}")
        return False
    return False


def formatear(o):
    e = html.escape
    lineas = [f"💼 <b>{e(o['titulo'])}</b>"]
    if o["empresa"]:
        lineas.append(f"🏢 {e(o['empresa'])}")
    if o["lugar"]:
        lineas.append(f"📍 {e(o['lugar'])}")
    if o["fecha"]:
        lineas.append(f"🗓 {e(o['fecha'])}")
    lineas.append(f"🔎 {e(o['fuente'])}")
    lineas.append(f'<a href="{e(o["link"])}">Ver oferta</a>')
    return "\n".join(lineas)


# ----------------------------------------------------------------------------
# PRINCIPAL
# ----------------------------------------------------------------------------
def main():
    global TELEGRAM_CHAT_ID
    dry_run = "--dry-run" in sys.argv

    if not dry_run:
        if not TELEGRAM_TOKEN:
            print("Falta el secreto TELEGRAM_TOKEN.")
            sys.exit(1)
        if not TELEGRAM_CHAT_ID:
            TELEGRAM_CHAT_ID = detectar_chat_id()
            if not TELEGRAM_CHAT_ID:
                print("No encontré el chat id: abre tu bot en Telegram, toca Iniciar, "
                      "escríbele 'hola' y vuelve a ejecutar.")
                sys.exit(1)
            print(f"Chat id detectado: {TELEGRAM_CHAT_ID}")

    if "--test" in sys.argv:
        ok = enviar_telegram("✅ Bot de ofertas funcionando correctamente.")
        print("Mensaje de prueba enviado." if ok else "No se pudo enviar. Revisa token y chat id.")
        return

    historial = cargar_historial()
    encontradas = {}

    for termino in BUSQUEDAS:
        for fuente in FUENTES:
            print(f"Buscando '{termino}' en {fuente.__name__.replace('buscar_', '')}...")
            try:
                for o in fuente(termino):
                    if titulo_valido(o["titulo"]):
                        encontradas.setdefault(o["id"], o)
            except Exception as ex:  # una fuente caída no detiene el resto
                print(f"  error inesperado: {ex}")

    # Quitar duplicados por título+empresa (misma oferta publicada en varias búsquedas)
    vistos, nuevas = set(), []
    for o in encontradas.values():
        clave = (o["titulo"].lower(), o["empresa"].lower())
        if o["id"] in historial or clave in vistos:
            continue
        vistos.add(clave)
        nuevas.append(o)

    print(f"\nTotal encontradas: {len(encontradas)} | Nuevas: {len(nuevas)}")

    if dry_run:
        for o in nuevas:
            print("-" * 60)
            print(formatear(o))
        return

    if nuevas:
        enviar_telegram(f"📢 <b>{len(nuevas)} ofertas nuevas</b> (Minas / Geomensura - Chile)")
    for o in nuevas:
        if enviar_telegram(formatear(o)):
            historial.add(o["id"])
        time.sleep(1.1)  # respeta el límite de Telegram

    guardar_historial(historial)
    print("Listo.")


if __name__ == "__main__":
    main()
