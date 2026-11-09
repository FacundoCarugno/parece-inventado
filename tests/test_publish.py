"""Tests del publicador contra un Meta simulado (tests/mock_meta.py). No llaman nunca a Meta de verdad.

    python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(AQUI))

import publish  # noqa: E402
from mock_meta import crear_servidor  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = crear_servidor()
        self.mock = self.srv.estado
        base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = Path(tempfile.mkdtemp())
        cal = json.loads((AQUI / "fixture_schedule.json").read_text(encoding="utf-8"))
        cal["pages_base"] = base
        (self.tmp / "schedule.json").write_text(json.dumps(cal), encoding="utf-8")
        self.env_prev = dict(os.environ)
        os.environ.update(
            SCHEDULE=str(self.tmp / "schedule.json"),
            LOGS_DIR=str(self.tmp / "logs"),
            META_BASE=f"{base}/v21.0",
            IG_ACCESS_TOKEN="TEST_TOKEN",
            IG_USER_ID="17840000000000001",
            NO_SLEEP="1",
            URL_WAIT="0",
            NOW_OVERRIDE="2026-11-10T19:05:00-03:00",
        )
        self.probar_original = publish.probar_video
        publish.probar_video = lambda ruta: "video simulado"  # el video real se valida en TestVideosReales

    def tearDown(self):
        publish.probar_video = self.probar_original
        os.environ.clear()
        os.environ.update(self.env_prev)
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cal(self):
        return json.loads((self.tmp / "schedule.json").read_text(encoding="utf-8"))

    def dia(self, n):
        return next(d for d in self.cal()["days"] if d["day"] == n)

    def editar(self, n, **campos):
        c = self.cal()
        next(d for d in c["days"] if d["day"] == n).update(campos)
        (self.tmp / "schedule.json").write_text(json.dumps(c), encoding="utf-8")

    def publicaciones(self):
        return [l for l in self.mock.llamadas if l[1].endswith("media_publish")]


class TestPublicar(Base):
    def test_publicacion_correcta(self):
        self.assertEqual(publish.main([]), 0)
        d = self.dia(1)
        self.assertEqual(d["publish_status"], "published")
        self.assertEqual(d["comment_status"], "posted")
        self.assertEqual(d["pin_status"], "not_supported_by_api")
        self.assertTrue(d["media_id"])
        self.assertEqual(len(self.publicaciones()), 1)
        self.assertEqual(self.mock.comentarios, [(d["media_id"], "¿La conocías?")])
        self.assertEqual(self.dia(2)["publish_status"], "pending")
        c = self.mock.contenedores[d["container_id"]]
        self.assertTrue(c["video_url"].endswith("/videos/historia-01.mp4"))

    def test_reintentos_en_errores_temporales(self):
        self.mock.fallos.update(create_500=2, status_seq=["IN_PROGRESS", "IN_PROGRESS"])
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.dia(1)["publish_status"], "published")
        self.assertEqual(len(self.publicaciones()), 1)

    def test_no_duplica_el_mismo_dia(self):
        self.assertEqual(publish.main([]), 0)
        os.environ["NOW_OVERRIDE"] = "2026-11-10T19:31:00-03:00"  # corre el respaldo de las 19:30
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(len(self.publicaciones()), 1)
        self.assertEqual(self.dia(2)["publish_status"], "pending")

    def test_recupera_si_se_corto_despues_de_publicar(self):
        self.mock.fallos["publish_500_after"] = True
        self.assertEqual(publish.main([]), 0)
        d = self.dia(1)
        self.assertEqual(d["publish_status"], "published")
        self.assertEqual(len(self.mock.medios), 1)

    def test_recupera_estado_publishing_sin_duplicar(self):
        # simula que el proceso murió justo después de media_publish: el Reel ya está en la cuenta
        self.mock.medios.insert(0, {"id": "999", "caption": "Historia 1 · Prueba uno\n\nTexto", "timestamp": "x"})
        self.editar(1, publish_status="publishing", container_id="555")
        self.assertEqual(publish.main([]), 0)
        d = self.dia(1)
        self.assertEqual((d["publish_status"], d["media_id"]), ("published", "999"))
        self.assertEqual(self.publicaciones(), [])
        self.assertEqual(self.mock.comentarios, [("999", "¿La conocías?")])

    def test_recupera_contenedor_ya_creado(self):
        self.mock.contenedores["777"] = {"caption": self.dia(1)["caption"], "video_url": "x", "consultas": 0, "publicado": False}
        self.editar(1, publish_status="container_created", container_id="777")
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.dia(1)["publish_status"], "published")
        creados = [l for l in self.mock.llamadas if l == ("POST", "17840000000000001/media")]
        self.assertEqual(creados, [])

    def test_dia_atrasado_se_publica_tarde_sin_saltarlo(self):
        os.environ["NOW_OVERRIDE"] = "2026-11-11T19:05:00-03:00"  # el día 1 (10/11) no se publicó
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.dia(1)["publish_status"], "published")
        self.assertEqual(self.dia(2)["publish_status"], "pending")  # uno por fecha: el 2 sale mañana
        os.environ["NOW_OVERRIDE"] = "2026-11-12T19:05:00-03:00"
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.dia(2)["publish_status"], "published")

    def test_video_no_listo_no_salta_ni_sustituye(self):
        self.editar(1, publish_status="published", published_at="2026-11-10T19:01:00-03:00", comment_status="posted", media_id="1")
        self.editar(2, publish_status="published", published_at="2026-11-11T19:01:00-03:00", comment_status="posted", media_id="2")
        os.environ["NOW_OVERRIDE"] = "2026-11-12T19:05:00-03:00"
        self.assertEqual(publish.main([]), 1)  # el 3 no tiene video: falla y abre Issue
        d = self.dia(3)
        self.assertEqual(d["publish_status"], "pending")
        self.assertIn("no está listo", d["last_error"])
        self.assertEqual(self.publicaciones(), [])

    def test_demasiado_temprano_no_hace_nada(self):
        os.environ["NOW_OVERRIDE"] = "2026-11-10T10:00:00-03:00"
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.mock.llamadas, [])

    def test_fecha_futura_no_se_adelanta(self):
        os.environ["NOW_OVERRIDE"] = "2026-11-09T19:05:00-03:00"
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.dia(1)["publish_status"], "pending")

    def test_contenedor_con_error(self):
        self.mock.fallos["status_seq"] = ["ERROR"]
        self.assertEqual(publish.main([]), 1)
        d = self.dia(1)
        self.assertEqual((d["publish_status"], d["container_id"]), ("pending", None))
        self.assertEqual(self.publicaciones(), [])
        self.mock.fallos.clear()
        os.environ["NOW_OVERRIDE"] = "2026-11-10T19:31:00-03:00"  # el respaldo lo vuelve a intentar
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.dia(1)["publish_status"], "published")

    def test_comentario_fallido_se_reintenta(self):
        self.mock.fallos["comment_500"] = 1
        self.assertEqual(publish.main([]), 1)
        self.assertEqual(self.dia(1)["publish_status"], "published")
        self.assertEqual(self.dia(1)["comment_status"], "failed")
        os.environ["NOW_OVERRIDE"] = "2026-11-10T19:31:00-03:00"
        self.assertEqual(publish.main([]), 0)
        self.assertEqual(self.dia(1)["comment_status"], "posted")
        self.assertEqual(len(self.publicaciones()), 1)

    def test_dry_run_no_publica(self):
        self.assertEqual(publish.main(["--dry-run"]), 0)
        self.assertEqual(self.mock.llamadas, [])
        self.assertEqual(self.dia(1)["attempts"], 0)

    def test_token_invalido(self):
        os.environ["IG_ACCESS_TOKEN"] = "otro"
        self.assertEqual(publish.main(["--whoami"]), 1)
        os.environ["IG_ACCESS_TOKEN"] = "TEST_TOKEN"
        self.assertEqual(publish.main(["--whoami"]), 0)


class TestVideosReales(unittest.TestCase):
    """Valida cada video del calendario real que figure como listo (h264, 1080x1920, 3-90 s) y su caption."""

    def test_calendario_real(self):
        cal = json.loads((RAIZ / "schedule.json").read_text(encoding="utf-8"))
        vistos = set()
        for d in cal["days"]:
            with self.subTest(dia=d["day"]):
                self.assertNotIn(d["day"], vistos)
                vistos.add(d["day"])
                self.assertLess(len(d["caption"]), 2200)
                self.assertLessEqual(d["caption"].count("#"), 30)
                self.assertTrue(d["caption"].startswith(f"Historia {d['day']} · "))
                if d.get("generation_status") == "ready":
                    publish.probar_video(RAIZ / d["video"])


if __name__ == "__main__":
    unittest.main()
