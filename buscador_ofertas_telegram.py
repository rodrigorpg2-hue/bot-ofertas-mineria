"""
Buscador de ofertas laborales -> Telegram
Perfiles por defecto: Ingeniero Civil en Minas / Ingeniero Geomensor (Chile)
Fuentes: LinkedIn (búsqueda pública) y Computrabajo Chile

Requisitos:
    pip install requests beautifulsoup4

Variables de entorno:
    TELEGRAM_TOKEN   -> token del bot (te lo da @BotFather)
    TELEGRAM_CHAT_ID -> tu chat id (si falta, se detecta del último mensaje al bot)

Uso por consola (GitHub Actions o PC):
    python buscador_ofertas_telegram.py            # busca y envía ofertas nuevas
    python buscador_ofertas_telegram.py --todas    # todas las disponibles en Chile
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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Términos de búsqueda por defecto (desde Telegram se pueden cambiar)
BUSQUEDAS = [
    "ingeniero civil en minas",
    "ingeniero civil de minas",
    "ingeniero en minas",
    "ingeniero geomensor",
    "geomensor",
]

UBICACION = "Chile"

# Modo "nuevas": antigüedad máxima en LinkedIn
# r86400 = 24 h | r604800 = 7 días | r2592000 = 30 días
LINKEDIN_ANTIGUEDAD = os.getenv("LINKEDIN_ANTIGUEDAD", "r604800")
LINKEDIN_PAGINAS = 4          # 25 ofertas por página
COMPUTRABAJO_PAGINAS = 3      # ~20 ofertas por página

# Modo "todas": sin filtro de fecha y más páginas
LINKEDIN_PAGINAS_TODAS = 10
COMPUTRABAJO_PAGINAS_TODAS = 8

# Palabras que deben aparecer en el título (además de las de tus términos).
PALABRAS_CLAVE_BASE = ["minas", "mina", "minero", "minera", "minería", "mineria",
                       "geomensor", "geomensura", "topógraf", "topograf"]

# Palabras demasiado generales que no sirven como filtro por sí solas
PALABRAS_GENERICAS = {"ingeniero", "ingeniera", "ingenieria", "ingeniería", "civil",
                      "de", "del", "en", "la", "el", "los", "las", "y", "para", "con",
                      "jefe", "senior", "junior", "analista", "tecnico", "técnico",
                      "supervisor", "especialista"}

PALABRAS_EXCLUIDAS = ["práctica", "practica", "practicante", "alumno en práctica"]

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
    lista = list(ids)[-5000:]
    ARCHIVO_HISTORIAL.write_text(json.dumps(lista, ensure_ascii=False), encoding="utf-8")


def palabras_clave(terminos):
    """Palabras que validan un título: las base + las palabras propias de cada término."""
    claves = set(PALABRAS_CLAVE_BASE)
    for t in terminos:
        utiles = [p for p in re.findall(r"\w+", t.lower())
                  if len(p) >= 4 and p not in PALABRAS_GENERICAS]
        if utiles:
            claves.update(utiles)
        else:  # término solo con palabras genéricas: se exige la frase completa
            claves.add(t.lower().strip())
    return claves


def titulo_valido(titulo, claves):
    t = titulo.lower()
    if any(p in t for p in PALABRAS_EXCLUIDAS):
        return False
    return any(p in t for p in claves)


def _get(url):
    """GET que devuelve (respuesta | None, descripción del problema | None)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        return None, f"sin conexión ({type(e).__name__})"
    if r.status_code != 200:
        return None, f"respondió {r.status_code}" + (" (bloqueo)" if r.status_code in (403, 429, 999) else "")
    return r, None


# ----------------------------------------------------------------------------
# FUENTE 1: LINKEDIN (endpoint público de búsqueda, sin login)
# ----------------------------------------------------------------------------
def buscar_linkedin(termino, antiguedad, paginas):
    ofertas, problema = [], None
    for pagina in range(paginas):
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
               f"?keywords={quote_plus(termino)}&location={quote_plus(UBICACION)}"
               f"&start={pagina * 25}")
        if antiguedad:
            url += f"&f_TPR={antiguedad}"
        r, problema = _get(url)
        if not r or not r.text.strip():
            break
        tarjetas = BeautifulSoup(r.text, "html.parser").select("li")
        if not tarjetas:
            break
        for li in tarjetas:
            a = li.select_one("a.base-card__full-link") or li.select_one("a")
            titulo = li.select_one("h3")
            if not a or not titulo:
                continue
            link = a.get("href", "").split("?")[0]
            m = re.search(r"(\d{8,})", link)
            empresa = li.select_one("h4")
            lugar = li.select_one(".job-search-card__location")
            fecha = li.select_one("time")
            ofertas.append({
                "id": f"li-{m.group(1) if m else link}",
                "fuente": "LinkedIn",
                "titulo": limpiar(titulo.get_text()),
                "empresa": limpiar(empresa.get_text()) if empresa else "",
                "lugar": limpiar(lugar.get_text()) if lugar else "",
                "fecha": fecha.get("datetime", "") if fecha else "",
                "link": link,
            })
        time.sleep(1.5)
    return ofertas, problema


# ----------------------------------------------------------------------------
# FUENTE 2: COMPUTRABAJO CHILE
# ----------------------------------------------------------------------------
def buscar_computrabajo(termino, antiguedad, paginas):
    ofertas, problema = [], None
    slug = quote(termino.lower().strip().replace(" ", "-"))
    for pagina in range(1, paginas + 1):
        url = f"https://cl.computrabajo.com/trabajo-de-{slug}"
        if pagina > 1:
            url += f"?p={pagina}"
        r, problema = _get(url)
        if not r:
            break
        tarjetas = BeautifulSoup(r.text, "html.parser").select("article.box_offer")
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
            empresa = art.select_one("p a") or art.select_one("p.fs16 a")
            lugar = art.select_one("p span.mr10") or art.select_one("p.fs16 span")
            fecha = art.select_one("p.fs13") or art.select_one("p.fc_aux")
            ofertas.append({
                "id": f"ct-{art.get('data-id') or link}",
                "fuente": "Computrabajo",
                "titulo": limpiar(a.get_text()),
                "empresa": limpiar(empresa.get_text()) if empresa else "",
                "lugar": limpiar(lugar.get_text()) if lugar else "",
                "fecha": limpiar(fecha.get_text()) if fecha else "",
                "link": link,
            })
        time.sleep(1.5)
    return ofertas, problema


FUENTES = {"LinkedIn": buscar_linkedin, "Computrabajo": buscar_computrabajo}


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


def enviar_telegram(texto, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    datos = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": texto,
             "parse_mode": "HTML", "disable_web_page_preview": True}
    for _ in range(3):
        try:
            r = requests.post(url, data=datos, timeout=20)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 200:
            return True
        if r.status_code == 429:
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


def formatear_compacto(i, o):
    e = html.escape
    extra = " · ".join(x for x in (o["empresa"], o["lugar"]) if x)
    return (f'{i}. <a href="{e(o["link"])}">{e(o["titulo"])}</a>'
            + (f"\n    {e(extra)}" if extra else "") + f" [{o['fuente']}]")


def enviar_listado(ofertas, titulo):
    """Envía muchas ofertas agrupadas (10 por mensaje) para no saturar el chat."""
    enviar_telegram(f"📋 <b>{titulo}</b>: {len(ofertas)} ofertas")
    for inicio in range(0, len(ofertas), 10):
        bloque = ofertas[inicio:inicio + 10]
        texto = "\n\n".join(formatear_compacto(inicio + j + 1, o) for j, o in enumerate(bloque))
        enviar_telegram(texto)
        time.sleep(1.1)


# ----------------------------------------------------------------------------
# BÚSQUEDA
# ----------------------------------------------------------------------------
def ejecutar_busqueda(terminos=None, modo="nuevas", usar_historial=True,
                      enviar=True, resumen=True):
    """
    modo="nuevas": últimas ofertas (filtro de fecha de LinkedIn) y solo las no enviadas.
    modo="todas":  todas las disponibles en Chile, sin filtro de fecha ni historial.
    Devuelve un dict con estadísticas.
    """
    terminos = terminos or BUSQUEDAS
    todas = modo == "todas"
    claves = palabras_clave(terminos)
    paginas = {"LinkedIn": LINKEDIN_PAGINAS_TODAS if todas else LINKEDIN_PAGINAS,
               "Computrabajo": COMPUTRABAJO_PAGINAS_TODAS if todas else COMPUTRABAJO_PAGINAS}
    antiguedad = None if todas else LINKEDIN_ANTIGUEDAD

    historial = cargar_historial() if (usar_historial and not todas) else set()
    encontradas = {}
    stats = {"modo": modo, "por_fuente": {}, "problemas": {}}

    for termino in terminos:
        for nombre, fuente in FUENTES.items():
            print(f"Buscando '{termino}' en {nombre}...")
            try:
                ofertas, problema = fuente(termino, antiguedad, paginas[nombre])
            except Exception as ex:
                ofertas, problema = [], f"error inesperado: {ex}"
            if problema:
                stats["problemas"].setdefault(nombre, problema)
                print(f"  [{nombre}] {problema}")
            validas = [o for o in ofertas if titulo_valido(o["titulo"], claves)]
            stats["por_fuente"][nombre] = stats["por_fuente"].get(nombre, 0) + len(validas)
            for o in validas:
                encontradas.setdefault(o["id"], o)

    vistos, resultado = set(), []
    for o in encontradas.values():
        clave = (o["titulo"].lower(), o["empresa"].lower())
        if o["id"] in historial or clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(o)

    stats["encontradas"] = len(encontradas)
    stats["enviadas"] = len(resultado)
    print(f"\nTotal encontradas: {len(encontradas)} | A enviar: {len(resultado)}")

    if not enviar:
        for o in resultado:
            print("-" * 60)
            print(formatear(o))
        return stats

    if todas:
        if resultado:
            enviar_listado(resultado, "Todas las ofertas disponibles en Chile")
    else:
        if resultado:
            enviar_telegram(f"📢 <b>{len(resultado)} ofertas nuevas</b>")
        for o in resultado:
            if enviar_telegram(formatear(o)):
                historial.add(o["id"])
            time.sleep(1.1)
        if usar_historial:
            guardar_historial(historial)

    # resumen=True siempre avisa; resumen="auto" solo si hubo ofertas o problemas
    if resumen is True or (resumen == "auto" and (resultado or stats["problemas"])):
        enviar_telegram(texto_resumen(stats))
    return stats


def texto_resumen(stats):
    titulo = "todas las disponibles" if stats["modo"] == "todas" else "solo nuevas"
    lineas = [f"✅ <b>Búsqueda terminada</b> ({titulo})"]
    for nombre in FUENTES:
        n = stats["por_fuente"].get(nombre, 0)
        prob = stats["problemas"].get(nombre)
        lineas.append(f"• {nombre}: {n} ofertas" + (f" ⚠️ {html.escape(prob)}" if prob else ""))
    if stats["enviadas"] == 0:
        lineas.append("\nNo hay ofertas nuevas por ahora." if stats["modo"] == "nuevas"
                      else "\nNo encontré ofertas con estos términos.")
    else:
        lineas.append(f"\n📨 Enviadas: {stats['enviadas']}")
    return "\n".join(lineas)


# ----------------------------------------------------------------------------
# PRINCIPAL (uso por consola / GitHub Actions)
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

    modo = "todas" if "--todas" in sys.argv else "nuevas"
    # En ejecuciones automáticas sin ofertas nuevas no se envía resumen (evita mensajes vacíos)
    ejecutar_busqueda(modo=modo, enviar=not dry_run, resumen=False)
    print("Listo.")


if __name__ == "__main__":
    main()
