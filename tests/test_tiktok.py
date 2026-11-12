"""Tests de tiktok.py contra un TikTok simulado (nunca llama a TikTok de verdad)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

import publish  # noqa: E402
import tiktok  # noqa: E402


def servidor():
    estado = {"subidas": [], "renovaciones": 0, "init": 0, "fallar_init": False, "estado": "SEND_TO_USER_INBOX"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            cuerpo = self.rfile.read(n)
            if self.path == "/v2/oauth/token/":
                estado["renovaciones"] += 1
                return self._json(200, {"access_token": "nuevo_acceso", "expires_in": 86400, "refresh_token": "nuevo_refresh", "refresh_expires_in": 31536000, "open_id": "u1", "scope": "video.upload"})
            if self.headers.get("Authorization") not in ("Bearer acceso", "Bearer nuevo_acceso"):
                return self._json(401, {"error": {"code": "access_token_invalid"}})
            if self.path == "/v2/post/publish/inbox/video/init/":
                estado["init"] += 1
                if estado["fallar_init"]:
                    return self._json(200, {"error": {"code": "spam_risk_too_many_pending_share", "message": "demasiados borradores"}})
                puerto = self.server.server_address[1]
                return self._json(200, {"data": {"publish_id": "p_1", "upload_url": f"http://127.0.0.1:{puerto}/subir"}, "error": {"code": "ok"}})
            if self.path == "/v2/post/publish/status/fetch/":
                return self._json(200, {"data": {"status": estado["estado"], "fail_reason": "x"}, "error": {"code": "ok"}})
            self._json(404, {})

        def do_PUT(self):
            n = int(self.headers.get("Content-Length", 0))
            estado["subidas"].append((self.headers.get("Content-Range"), len(self.rfile.read(n))))
            self.send_response(201)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.estado = estado
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TestTikTok(unittest.TestCase):
    def setUp(self):
        self.srv = servidor()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "videos").mkdir()
        (self.tmp / "videos" / "historia-01.mp4").write_bytes(b"x" * 1234)
        cal = json.loads((AQUI / "fixture_schedule.json").read_text(encoding="utf-8"))
        cal["days"][0].update(publish_status="published", published_at="2026-11-10T19:01:00-03:00")
        cal["tiktok_desde"] = 1
        (self.tmp / "schedule.json").write_text(json.dumps(cal), encoding="utf-8")
        self.env_prev = dict(os.environ)
        os.environ.update(SCHEDULE=str(self.tmp / "schedule.json"), LOGS_DIR=str(self.tmp / "logs"), NO_SLEEP="1",
                          NOW_OVERRIDE="2026-11-10T19:10:00-03:00", TIKTOK_BASE=f"http://127.0.0.1:{self.srv.server_address[1]}",
                          TIKTOK_KEY="clave-de-prueba", TIKTOK_CLIENT_KEY="ck", TIKTOK_CLIENT_SECRET="cs")
        self.token = self.tmp / "tiktok_token.enc"
        self.token_prev = tiktok.TOKEN
        tiktok.TOKEN = self.token
        tiktok.cifrar({"access_token": "acceso", "expires_at": time.time() + 3600, "refresh_token": "r", "open_id": "u1"}, self.token)

    def tearDown(self):
        tiktok.TOKEN = self.token_prev
        os.environ.clear()
        os.environ.update(self.env_prev)
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dia(self, n):
        return next(d for d in json.loads((self.tmp / "schedule.json").read_text(encoding="utf-8"))["days"] if d["day"] == n)

    def test_manda_borrador(self):
        self.assertEqual(tiktok.main([]), 0)
        d = self.dia(1)
        self.assertEqual((d["tiktok_status"], d["tiktok_publish_id"]), ("sent", "p_1"))
        self.assertEqual(self.srv.estado["subidas"], [("bytes 0-1233/1234", 1234)])

    def test_uno_por_fecha(self):
        self.assertEqual(tiktok.main([]), 0)
        self.assertEqual(tiktok.main([]), 0)
        self.assertEqual(self.srv.estado["init"], 1)

    def test_no_manda_si_instagram_no_publico(self):
        c = json.loads((self.tmp / "schedule.json").read_text(encoding="utf-8"))
        c["days"][0]["publish_status"] = "pending"
        (self.tmp / "schedule.json").write_text(json.dumps(c), encoding="utf-8")
        self.assertEqual(tiktok.main([]), 0)
        self.assertEqual(self.srv.estado["init"], 0)

    def test_renueva_token_vencido_y_lo_guarda_cifrado(self):
        tiktok.cifrar({"access_token": "viejo", "expires_at": time.time() - 10, "refresh_token": "r", "open_id": "u1"}, self.token)
        self.assertEqual(tiktok.main([]), 0)
        self.assertEqual(self.srv.estado["renovaciones"], 1)
        self.assertNotIn(b"nuevo_refresh", self.token.read_bytes())
        self.assertEqual(tiktok.descifrar(self.token)["refresh_token"], "nuevo_refresh")

    def test_error_de_tiktok_queda_registrado(self):
        self.srv.estado["fallar_init"] = True
        self.assertEqual(tiktok.main([]), 1)
        d = self.dia(1)
        self.assertEqual(d["tiktok_status"], "failed")
        self.assertIn("demasiados borradores", d["tiktok_errors"][0]["error"])

    def test_sin_conectar_no_hace_nada(self):
        c = json.loads((self.tmp / "schedule.json").read_text(encoding="utf-8"))
        c.pop("tiktok_desde")
        (self.tmp / "schedule.json").write_text(json.dumps(c), encoding="utf-8")
        self.assertEqual(tiktok.main([]), 0)
        self.assertEqual(self.srv.estado["init"], 0)


if __name__ == "__main__":
    unittest.main()
