"""
Bot que revisa la cartelera de Showcase y avisa por Telegram cuando
una película aparece en la sala IMAX (con o sin venta anticipada).

Cómo detecta: en todoshowcase.com las películas de IMAX tienen links con
house_id=3250 (la sala IMAX de Norcenter). El bot lee esos links, así que no
se confunde con las películas de otras salas.

Instalación (solo la librería estándar alcanza; Playwright es un plan B):
    pip install playwright        # opcional
    playwright install chromium   # opcional

Variables de entorno necesarias (para las alertas):
    TELEGRAM_TOKEN   -> el token que te da @BotFather al crear el bot
    TELEGRAM_CHAT_ID -> tu chat id (podés obtenerlo con @userinfobot)
    Importante: después de crear el bot, abrilo en Telegram y mandale /start
    una vez, si no, no puede escribirte.

Uso:
    python bot_showcase_imax.py            # vigila en bucle y avisa
    python bot_showcase_imax.py --prueba   # solo muestra qué ve en IMAX, sin avisar
    python bot_showcase_imax.py --una-vez  # un solo chequeo (para GitHub Actions)

Códigos de salida de --una-vez:
    0  = todavía no aparece (todo normal)
    10 = apareció y el aviso por Telegram se envió
    2  = no vi ninguna película IMAX (la página cambió o bloquea la consulta)
    1  = error (por ejemplo, no se pudo enviar el aviso)
"""

import html
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- CONFIG ----
TITULOS = ["Dune", "Duna"]  # nombres con los que puede aparecer la película
URL = "https://www.todoshowcase.com/"  # sin parámetros de tracking (gclid, etc.)
IMAX_HOUSE_ID = "3250"  # id de la sala IMAX Theatre en los links de Showcase
MINUTOS_ENTRE_CHEQUEOS = 15
# -----------------------------------------------------------------------------

ANCLA = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
ETIQUETAS = re.compile(r"<[^>]+>")
CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-AR,es;q=0.9",
}


def normalizar(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar sin problemas."""
    texto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def limpiar(fragmento: str) -> str:
    """Saca etiquetas HTML y espacios sobrantes."""
    return " ".join(html.unescape(ETIQUETAS.sub(" ", fragmento)).split())


def obtener_html_simple(url: str) -> str:
    req = urllib.request.Request(url, headers=CABECERAS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def obtener_html_navegador(url: str) -> str:
    """Plan B: navegador real, por si la página carga la cartelera con JavaScript."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.goto(url, timeout=60_000)
        pagina.wait_for_load_state("networkidle", timeout=60_000)
        contenido = pagina.content()
        navegador.close()
    return contenido


def peliculas_imax(contenido: str) -> dict:
    """
    Devuelve {filmid: {"titulo": str, "anticipada": bool}} con las películas
    cuyos links apuntan a la sala IMAX. La etiqueta ANTICIPADA aparece como
    texto justo antes del link de cada película.
    """
    peliculas = {}
    fin_anterior = 0
    for m in ANCLA.finditer(contenido):
        href = html.unescape(m.group(1))
        previo = normalizar(limpiar(contenido[fin_anterior : m.start()]))
        fin_anterior = m.end()

        film = re.search(r"filmid=(\d+)", href)
        casa = re.search(r"house_id=(\d+)", href)
        if not film or not casa or casa.group(1) != IMAX_HOUSE_ID:
            continue

        datos = peliculas.setdefault(film.group(1), {"titulo": "", "anticipada": False})
        texto = limpiar(m.group(2))
        if len(texto) > len(datos["titulo"]):
            datos["titulo"] = texto
        if "anticipada" in previo[-40:]:
            datos["anticipada"] = True
    return peliculas


def coincide(titulo: str, titulos: list[str]) -> bool:
    """True si el título contiene alguno de los nombres buscados como palabra completa."""
    titulo = normalizar(titulo)
    return any(
        re.search(rf"\b{re.escape(normalizar(t))}\b", titulo) for t in titulos
    )


def consultar_imax() -> dict:
    peliculas = peliculas_imax(obtener_html_simple(URL))
    if not peliculas:  # quizá la página es dinámica o bloqueó la consulta simple
        try:
            peliculas = peliculas_imax(obtener_html_navegador(URL))
        except ImportError:
            print("[!] No vi películas IMAX y Playwright no está instalado.")
    return peliculas


def avisar_telegram(mensaje: str) -> bool:
    """Devuelve True si el mensaje se envió de verdad."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[!] Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID. Aviso solo por consola:")
        print(mensaje)
        return False
    datos = urllib.parse.urlencode({"chat_id": chat_id, "text": mensaje}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=datos
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        json.load(resp)
    return True


def armar_aviso(film_id: str, p: dict) -> str:
    estado = "con VENTA ANTICIPADA" if p["anticipada"] else "en cartelera"
    link = (
        "https://entradas.todoshowcase.com/showcase/pelicula"
        f"?filmid={film_id}&house_id={IMAX_HOUSE_ID}"
    )
    return f"🎬 ¡{p['titulo']} ya aparece en IMAX en Showcase ({estado})!\n{link}"


def modo_prueba() -> None:
    peliculas = consultar_imax()
    if not peliculas:
        print("No encontré ninguna película IMAX. Puede que la página haya cambiado.")
        return
    print(f"Películas IMAX que veo ahora ({len(peliculas)}):")
    for p in peliculas.values():
        marca = "VENTA ANTICIPADA" if p["anticipada"] else "en cartelera"
        buscada = "  <-- ES LA QUE BUSCÁS" if coincide(p["titulo"], TITULOS) else ""
        print(f" - {p['titulo']}  [{marca}]{buscada}")


def una_vez() -> int:
    """Un solo chequeo. Pensado para correr cada 15 min en GitHub Actions."""
    try:
        peliculas = consultar_imax()
        if not peliculas:
            print("[!] No vi ninguna película IMAX: la página cambió o bloquea la consulta.")
            return 2
        for film_id, p in peliculas.items():
            if coincide(p["titulo"], TITULOS):
                if avisar_telegram(armar_aviso(film_id, p)):
                    print("Detectada. Aviso enviado.")
                    return 10
                print("[!] Detectada, pero NO se pudo enviar el aviso (faltan credenciales).")
                return 1
        print(time.strftime("%H:%M"), "- todavía no aparece en IMAX.")
        return 0
    except Exception as error:
        print("Error en el chequeo:", error)
        return 1


def main() -> None:
    nombre = " / ".join(TITULOS)
    print(f"Vigilando '{nombre}' en IMAX cada {MINUTOS_ENTRE_CHEQUEOS} min...")
    while True:
        try:
            for film_id, p in consultar_imax().items():
                if coincide(p["titulo"], TITULOS):
                    avisar_telegram(armar_aviso(film_id, p))
                    print("Detectada. Aviso enviado, el bot se detiene.")
                    return
            print(time.strftime("%H:%M"), "- todavía no aparece en IMAX.")
        except Exception as error:  # la web puede fallar o cambiar
            print("Error en este chequeo:", error)

        # pequeña variación aleatoria para no consultar a intervalos exactos
        time.sleep(MINUTOS_ENTRE_CHEQUEOS * 60 + random.randint(0, 60))


if __name__ == "__main__":
    if "--prueba" in sys.argv:
        modo_prueba()
    elif "--una-vez" in sys.argv:
        sys.exit(una_vez())
    else:
        main()
