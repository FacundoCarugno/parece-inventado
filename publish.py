#!/usr/bin/env python3
"""Publica en Instagram el Reel del día (API oficial de Instagram: Graph API de Meta con inicio de sesión unificado).

    python publish.py              publica el Reel que toque hoy (espera hasta las 19:00 de Buenos Aires)
    python publish.py --dry-run    valida el próximo video y su URL pública, sin publicar nada
    python publish.py --force      no espera a las 19:00 ni mira la fecha planeada (solo para pruebas a mano)
    python publish.py --status     muestra el calendario y el estado de cada día
    python publish.py --whoami     comprueba el token (devuelve el usuario de Instagram)

Secretos (variables de entorno, en GitHub Secrets): IG_ACCESS_TOKEN (token de usuario del sistema, no vence) e
IG_USER_ID (ID de la cuenta de Instagram). Nunca se imprimen.
`schedule.json` es el calendario y el estado: se guarda después de cada paso para poder recuperarse si se corta.
Reglas: como máximo un Reel por fecha de Buenos Aires; nunca se salta un día (si uno se atrasa, se publica tarde);
antes de crear o publicar nada se revisan los últimos medios de la cuenta para no duplicar.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as hora_t, timedelta, timezone
from pathlib import Path

AR = timezone(timedelta(hours=-3), "America/Argentina/Buenos_Aires")  # Argentina no tiene horario de verano
RAIZ = Path(__file__).resolve().parent
MARGEN = timedelta(hours=3)  # como mucho espera 3 h hasta la hora de publicar
PIN = "not_supported_by_api"  # la API oficial no permite fijar comentarios: se fija a mano desde la app


class Fallo(Exception):
    pass


# ---------------------------------------------------------------- utilidades
def api_base():
    return os.environ.get("META_BASE", "https://graph.facebook.com/v21.0").rstrip("/")


def ahora():
    o = os.environ.get("NOW_OVERRIDE")  # solo para tests
    return datetime.fromisoformat(o).astimezone(AR) if o else datetime.now(AR)


def dormir(seg):
    if seg > 0 and not os.environ.get("NO_SLEEP"):
        time.sleep(seg)


def ruta_calendario():
    return Path(os.environ.get("SCHEDULE", RAIZ / "schedule.json"))


def cargar():
    return json.loads(ruta_calendario().read_text(encoding="utf-8"))


def guardar(cal):
    p = ruta_calendario()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def log(msg):
    linea = f"[{ahora().strftime('%Y-%m-%d %H:%M:%S')} AR] {msg}"
    print(linea, flush=True)
    carpeta = Path(os.environ.get("LOGS_DIR", RAIZ / "logs"))
    carpeta.mkdir(exist_ok=True)
    with open(carpeta / f"publish-{ahora().strftime('%Y-%m')}.log", "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def fecha_de(iso):
    return datetime.fromisoformat(iso).astimezone(AR).date()


def token():
    t = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not t:
        raise Fallo("Falta el secreto IG_ACCESS_TOKEN.")
    return t


def api(metodo, ruta, params=None, reintentar=True):
    """Llama a la API. Reintenta errores de red, 429 y 5xx (salvo reintentar=False). Nunca muestra el token."""
    p = dict(params or {})
    p["access_token"] = token()
    url = f"{api_base()}/{ruta}"
    datos = None
    if metodo == "GET":
        url += "?" + urllib.parse.urlencode(p)
    else:
        datos = urllib.parse.urlencode(p).encode()
    ultimo = ""
    for intento in range(4 if reintentar else 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=datos, method=metodo), timeout=90) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")[:400]
            ultimo = f"Meta respondió {e.code} en {metodo} {ruta}: {cuerpo}"
            if e.code < 500 and e.code != 429:
                raise Fallo(ultimo)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            ultimo = f"Error de red en {metodo} {ruta}: {e}"
        log(f"  reintento {intento + 1}: {ultimo}")
        dormir(float(os.environ.get("RETRY_SECONDS", 15)) * (intento + 1))
    raise Fallo(ultimo)


def usuario():
    uid = os.environ.get("IG_USER_ID", "").strip()
    if not uid:
        raise Fallo("Falta el secreto IG_USER_ID (el ID de la cuenta de Instagram).")
    return uid


# ---------------------------------------------------------------- validación del video
def sondear(ruta):
    """Devuelve (códec, ancho, alto, duración) con ffprobe, o con ffmpeg si no hay ffprobe."""
    ffprobe = os.environ.get("FFPROBE") or shutil.which("ffprobe")
    if ffprobe:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height:format=duration", "-of", "json", str(ruta)], capture_output=True, text=True, check=True).stdout
        j = json.loads(out)
        s = j["streams"][0]
        return s["codec_name"], int(s["width"]), int(s["height"]), float(j["format"]["duration"])
    ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        raise Fallo("No hay ffprobe ni ffmpeg para validar el video.")
    err = subprocess.run([ffmpeg, "-hide_banner", "-i", str(ruta)], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    v = re.search(r"Video: (\w+).*?, (\d+)x(\d+)", err)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if not v or not d:
        raise Fallo(f"No pude leer el video {ruta.name}.")
    return v.group(1), int(v.group(2)), int(v.group(3)), int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))


def probar_video(ruta):
    if not ruta.exists():
        raise Fallo(f"No existe el video {ruta}.")
    tam = ruta.stat().st_size
    if not 100_000 <= tam <= 300_000_000:
        raise Fallo(f"Tamaño raro del video {ruta.name}: {tam} bytes.")
    codec, w, h, dur = sondear(ruta)
    if codec != "h264" or (w, h) != (1080, 1920) or not 3 <= dur <= 90:
        raise Fallo(f"Video no válido {ruta.name}: {codec} {w}x{h} {dur:.1f}s (se pide h264 1080x1920 de 3 a 90 s).")
    return f"{codec} {w}x{h} {dur:.1f}s {tam / 1e6:.1f} MB"


def url_video(cal, d):
    return f"{cal['pages_base'].rstrip('/')}/{d['video']}"


def probar_url(url):
    espera = float(os.environ.get("URL_WAIT", 240))  # GitHub Pages tarda un poco en publicar
    fin = time.time() + espera
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
                if r.status == 200:
                    return
                motivo = f"respondió {r.status}"
        except urllib.error.HTTPError as e:
            motivo = f"respondió {e.code}"
        except urllib.error.URLError as e:
            motivo = str(e.reason)
        if time.time() >= fin:
            raise Fallo(f"La URL pública del video no responde ({motivo}): {url}")
        dormir(20)


# ---------------------------------------------------------------- publicación
def marcador(d):
    return d["caption"].split("\n", 1)[0].strip()  # «Historia N · Título»: identifica el Reel en la cuenta


def buscar_publicado(uid, d):
    r = api("GET", f"{uid}/media", {"fields": "id,caption,timestamp", "limit": 15})
    for m in r.get("data", []):
        if (m.get("caption") or "").split("\n", 1)[0].strip() == marcador(d):
            return m["id"]
    return None


def esperar_contenedor(cid):
    for _ in range(int(os.environ.get("POLL_TRIES", 60))):
        st = api("GET", cid, {"fields": "status_code,status"})
        code = st.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return code
        if code in ("ERROR", "EXPIRED"):
            raise Fallo(f"El contenedor {cid} quedó en {code}: {st.get('status', '')}")
        dormir(float(os.environ.get("POLL_SECONDS", 10)))
    raise Fallo(f"El contenedor {cid} no terminó de procesarse a tiempo.")


def marcar_publicado(cal, d, media_id, como):
    d.update(publish_status="published", media_id=media_id, published_at=ahora().isoformat(timespec="seconds"), pin_status=PIN)
    d.pop("last_error", None)
    guardar(cal)
    log(f"Historia {d['day']} publicada ({como}): media {media_id}")


def comentar(cal, d):
    if d.get("comment_status") == "posted" or not d.get("first_comment"):
        return
    try:
        r = api("POST", f"{d['media_id']}/comments", {"message": d["first_comment"]}, reintentar=False)
        d.update(comment_status="posted", comment_id=r.get("id"), pin_status=PIN)
        log(f"  comentario publicado ({r.get('id')}). Fijarlo: a mano desde la app (la API no lo permite).")
    except Fallo as e:
        d["comment_status"] = "failed"
        d.setdefault("errors", []).append({"at": ahora().isoformat(timespec="seconds"), "error": f"comentario: {e}"})
        guardar(cal)
        raise
    guardar(cal)


def publicar_dia(cal, d):
    uid = usuario()
    d["attempts"] = d.get("attempts", 0) + 1
    guardar(cal)
    try:
        ya = buscar_publicado(uid, d)  # si se cortó a mitad, puede que ya esté en la cuenta
        if ya:
            marcar_publicado(cal, d, ya, "recuperado: ya estaba en la cuenta")
        else:
            if d.get("container_id") and d.get("publish_status") in ("container_created", "publishing"):
                try:
                    esperar_contenedor(d["container_id"])
                    log(f"  reutilizo el contenedor {d['container_id']}")
                except Fallo as e:
                    log(f"  el contenedor anterior no sirve ({e}); creo otro")
                    d.update(container_id=None, publish_status="pending")
                    guardar(cal)
            if not d.get("container_id"):
                r = api("POST", f"{uid}/media", {"media_type": "REELS", "video_url": url_video(cal, d), "caption": d["caption"], "share_to_feed": "true"})
                d.update(container_id=r["id"], publish_status="container_created")
                guardar(cal)
                log(f"  contenedor creado: {r['id']}")
            try:
                esperar_contenedor(d["container_id"])
            except Fallo:
                d.update(container_id=None, publish_status="pending")
                guardar(cal)
                raise
            d["publish_status"] = "publishing"
            guardar(cal)
            try:
                r = api("POST", f"{uid}/media_publish", {"creation_id": d["container_id"]}, reintentar=False)
                marcar_publicado(cal, d, r["id"], "media_publish")
            except Fallo as e:  # puede haberse publicado igual: compruebo antes de dar por fallido
                dormir(float(os.environ.get("RETRY_SECONDS", 15)))
                ya = buscar_publicado(uid, d)
                if not ya:
                    raise
                marcar_publicado(cal, d, ya, f"recuperado tras error: {e}")
        comentar(cal, d)
    except Fallo as e:
        d.setdefault("errors", []).append({"at": ahora().isoformat(timespec="seconds"), "error": str(e)})
        d["last_error"] = str(e)
        guardar(cal)
        raise


def mostrar(cal):
    print(f"{'Día':>4} {'Fecha':10} {'Video':8} {'Publicación':17} {'Comentario':10} {'Int.':>4}  Título")
    for d in cal["days"]:
        print(f"{d['day']:>4} {d['date']:10} {d.get('generation_status', '?'):8} {d.get('publish_status', '?'):17} {d.get('comment_status', '?'):10} {d.get('attempts', 0):>4}  {d.get('title', '')}")


def ejecutar(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="no esperar a la hora (con --dry-run)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--whoami", action="store_true")
    a = ap.parse_args(argv)
    cal = cargar()
    if a.status:
        mostrar(cal)
        return 0
    if a.whoami:
        uid = usuario()
        r = api("GET", uid, {"fields": "id,username"})
        print(f"Token válido: @{r.get('username')} (id {r.get('id')})")
        return 0

    hoy = ahora().date()
    dias = cal["days"]
    # comentarios que quedaron pendientes de días ya publicados
    if not a.dry_run:
        for x in dias:
            if x.get("publish_status") == "published" and x.get("first_comment") and x.get("comment_status") != "posted":
                log(f"Reintento el comentario de la historia {x['day']}")
                comentar(cal, x)

    d = next((x for x in dias if x.get("publish_status") != "published"), None)
    if d is None:
        log("No queda nada por publicar en el calendario.")
        return 0
    if not a.force:
        if any(x.get("published_at") and fecha_de(x["published_at"]) == hoy for x in dias):
            log("Hoy ya se publicó un Reel: no publico otro.")
            return 0
        if datetime.fromisoformat(d["date"]).date() > hoy:
            log(f"La próxima es la historia {d['day']} ({d['date']}): hoy no toca nada.")
            return 0
        objetivo = datetime.combine(hoy, hora_t.fromisoformat(cal.get("publish_time", "19:00")), AR)
        falta = objetivo - ahora()
        if falta > MARGEN:
            log(f"Faltan {falta} para las {cal.get('publish_time', '19:00')}: demasiado temprano.")
            return 0
        if falta.total_seconds() > 0 and not (a.dry_run or a.no_wait):
            log(f"Espero {int(falta.total_seconds())} s hasta las {cal.get('publish_time', '19:00')}.")
            dormir(falta.total_seconds())

    log(f"Historia {d['day']} «{d.get('title', '')}» (planeada {d['date']}, intento {d.get('attempts', 0) + 1}).")
    try:
        if d.get("generation_status") != "ready":
            raise Fallo(f"El video de la historia {d['day']} no está listo (generation_status={d.get('generation_status')}). No salto el día: espero a que esté.")
        if len(d["caption"]) >= 2200 or d["caption"].count("#") > 30:
            raise Fallo("El caption pasa de 2200 caracteres o de 30 hashtags.")
        log(f"  video OK: {probar_video(ruta_calendario().parent / d['video'])}")
        url = url_video(cal, d)
        probar_url(url)
        log(f"  URL pública OK: {url}")
    except Fallo as e:
        if not a.dry_run:
            d.setdefault("errors", []).append({"at": ahora().isoformat(timespec="seconds"), "error": str(e)})
            d["last_error"] = str(e)
            guardar(cal)
        raise
    if a.dry_run:
        log("Dry-run: todo listo para publicar; no se publicó nada.")
        return 0
    publicar_dia(cal, d)
    return 0


def main(argv=None):
    try:
        return ejecutar(argv)
    except Fallo as e:
        log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
