#!/usr/bin/env python3
"""Manda el Reel del día a TikTok como BORRADOR (bandeja de entrada), con la API oficial (Content Posting API, Upload).

    python tiktok.py            manda a TikTok el último día ya publicado en Instagram que falte (uno por fecha)
    python tiktok.py --status   muestra qué días se mandaron a TikTok
    python tiktok.py --check    renueva el token y comprueba que la conexión funciona (no sube nada)

El dueño de la cuenta recibe una notificación en TikTok, pega la descripción (página hoy.html) y toca Publicar.
La API de borradores no permite poner la descripción ni comentar: eso se hace a mano en la app.

Secretos (GitHub Secrets): TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET y TIKTOK_KEY (clave para cifrar el token).
El token de TikTok se guarda CIFRADO en tiktok_token.enc (dura 24 h y se renueva solo; el de renovación, 1 año).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import publish  # reutiliza calendario, hora de Argentina y registro

RAIZ = Path(__file__).resolve().parent
TOKEN = RAIZ / "tiktok_token.enc"
Fallo = publish.Fallo


def api_base():
    return os.environ.get("TIKTOK_BASE", "https://open.tiktokapis.com").rstrip("/")


def openssl():
    for c in (os.environ.get("OPENSSL"), "openssl", r"C:\Program Files\Git\usr\bin\openssl.exe"):
        if not c:
            continue
        p = shutil.which(c) or (c if Path(c).exists() else None)
        if p:
            return p
    raise Fallo("No encuentro openssl para cifrar el token.")


def _clave():
    k = os.environ.get("TIKTOK_KEY", "").strip()
    if not k:
        raise Fallo("Falta el secreto TIKTOK_KEY.")
    return k


def cifrar(datos: dict, ruta=None):
    ruta = ruta or TOKEN
    env = dict(os.environ, TT_PASS=_clave())
    r = subprocess.run([openssl(), "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-a", "-A", "-pass", "env:TT_PASS"],
                       input=json.dumps(datos).encode(), capture_output=True, env=env)
    if r.returncode:
        raise Fallo("No pude cifrar el token de TikTok.")
    ruta.write_bytes(r.stdout + b"\n")


def descifrar(ruta=None):
    ruta = ruta or TOKEN
    if not ruta.exists():
        raise Fallo("No hay token de TikTok guardado (falta conectar la cuenta).")
    env = dict(os.environ, TT_PASS=_clave())
    r = subprocess.run([openssl(), "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-a", "-A", "-pass", "env:TT_PASS"],
                       input=ruta.read_bytes().strip(), capture_output=True, env=env)
    if r.returncode:
        raise Fallo("No pude descifrar el token de TikTok: ¿TIKTOK_KEY es la misma con la que se conectó la cuenta?")
    return json.loads(r.stdout)


def pedir(metodo, url, cuerpo=None, form=None, token=None, datos=None, cabeceras=None):
    h = dict(cabeceras or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    if form is not None:
        datos = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif cuerpo is not None:
        datos = json.dumps(cuerpo).encode()
        h["Content-Type"] = "application/json; charset=UTF-8"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=datos, method=metodo, headers=h), timeout=300) as r:
            texto = r.read()
            return r.status, (json.loads(texto) if texto[:1] in (b"{", b"[") else {})
    except urllib.error.HTTPError as e:
        cuerpo_err = e.read().decode("utf-8", "replace")[:400]
        raise Fallo(f"TikTok respondió {e.code} en {urllib.parse.urlparse(url).path}: {cuerpo_err}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise Fallo(f"Error de red con TikTok: {e}")


def _ok(resp, que):
    err = resp.get("error") or {}
    if err.get("code") not in (None, "ok"):
        raise Fallo(f"TikTok rechazó {que}: {err.get('code')} {err.get('message', '')}")
    return resp.get("data") or {}


def renovar_si_hace_falta(t):
    if t.get("expires_at", 0) - time.time() > 600:
        return t
    clave = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    secreto = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    if not clave or not secreto:
        raise Fallo("Faltan los secretos TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET.")
    _, r = pedir("POST", f"{api_base()}/v2/oauth/token/", form={"client_key": clave, "client_secret": secreto, "grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
    if "access_token" not in r:
        raise Fallo(f"No pude renovar el token de TikTok: {r.get('error')} {r.get('error_description', '')}")
    ahora = time.time()
    t.update(access_token=r["access_token"], expires_at=ahora + int(r.get("expires_in", 86400)),
             refresh_token=r.get("refresh_token", t["refresh_token"]), refresh_expires_at=ahora + int(r.get("refresh_expires_in", 31536000)),
             open_id=r.get("open_id", t.get("open_id")), scope=r.get("scope", t.get("scope")))
    cifrar(t)
    publish.log("  TikTok: token renovado (guardado cifrado).")
    return t


def mandar_borrador(t, ruta):
    tam = ruta.stat().st_size
    if tam > 64 * 1024 * 1024:
        raise Fallo("El video pasa de 64 MB: hace falta subirlo en partes.")
    _, r = pedir("POST", f"{api_base()}/v2/post/publish/inbox/video/init/", token=t["access_token"],
                 cuerpo={"source_info": {"source": "FILE_UPLOAD", "video_size": tam, "chunk_size": tam, "total_chunk_count": 1}})
    d = _ok(r, "iniciar la subida")
    pid, url = d["publish_id"], d["upload_url"]
    st, _ = pedir("PUT", url, datos=ruta.read_bytes(), cabeceras={"Content-Type": "video/mp4", "Content-Length": str(tam), "Content-Range": f"bytes 0-{tam - 1}/{tam}"})
    if st not in (200, 201):
        raise Fallo(f"TikTok respondió {st} al subir el video.")
    for _ in range(int(os.environ.get("POLL_TRIES", 60))):
        _, r = pedir("POST", f"{api_base()}/v2/post/publish/status/fetch/", token=t["access_token"], cuerpo={"publish_id": pid})
        s = _ok(r, "consultar el estado")
        estado = s.get("status")
        if estado in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return pid, estado
        if estado == "FAILED":
            raise Fallo(f"TikTok no aceptó el video: {s.get('fail_reason')}")
        publish.dormir(float(os.environ.get("POLL_SECONDS", 10)))
    raise Fallo("TikTok no terminó de procesar el video a tiempo.")


def ejecutar(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true", help="no mirar si hoy ya se mandó uno")
    a = ap.parse_args(argv)
    cal = publish.cargar()
    desde = cal.get("tiktok_desde")
    if a.status:
        for d in cal["days"]:
            print(f"{d['day']:>4} {d['date']}  Instagram: {d.get('publish_status', '?'):10}  TikTok: {d.get('tiktok_status', '-')}")
        return 0
    if not desde:
        publish.log("TikTok todavía no está conectado (falta tiktok_desde en schedule.json): no hago nada.")
        return 0
    t = descifrar()
    if a.check:  # no renueva: la renovación solo la hace la publicación, que guarda el token nuevo
        dias_rest = int((t.get("refresh_expires_at", 0) - time.time()) / 86400)
        if dias_rest < 1:
            raise Fallo("El permiso de TikTok venció: hay que volver a conectar la cuenta.")
        print(f"Token de TikTok OK (permisos: {t.get('scope')}; la conexión vence en {dias_rest} días).")
        return 0
    t = renovar_si_hace_falta(t)
    hoy = publish.ahora().date()
    dias = cal["days"]
    if not a.force and any(x.get("tiktok_sent_at") and not x.get("tiktok_puesta_al_dia") and publish.fecha_de(x["tiktok_sent_at"]) == hoy for x in dias):
        publish.log("TikTok: hoy ya se mandó un borrador.")
        return 0
    d = next((x for x in dias if x["day"] >= desde and x.get("publish_status") == "published" and x.get("tiktok_status") != "sent"), None)
    if d is None:
        publish.log("TikTok: no hay nada nuevo para mandar (primero se publica en Instagram).")
        return 0
    publish.log(f"TikTok: mando la historia {d['day']} como borrador.")
    try:
        pid, estado = mandar_borrador(t, publish.ruta_calendario().parent / d["video"])
    except Fallo as e:
        d["tiktok_status"] = "failed"
        d.setdefault("tiktok_errors", []).append({"at": publish.ahora().isoformat(timespec="seconds"), "error": str(e)})
        publish.guardar(cal)
        raise
    d.update(tiktok_status="sent", tiktok_publish_id=pid, tiktok_sent_at=publish.ahora().isoformat(timespec="seconds"))
    publish.guardar(cal)
    publish.log(f"  TikTok: borrador enviado ({estado}). Falta publicarlo desde la app con la descripción de hoy.html.")
    return 0


def main(argv=None):
    try:
        return ejecutar(argv)
    except Fallo as e:
        publish.log(f"ERROR TikTok: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
