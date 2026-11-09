"""Servidor que imita la API de Instagram (graph.instagram.com) para los tests. Nunca toca Meta de verdad.

Fallos que se pueden provocar (atributo `fallos` del servidor):
  create_500: n            las n primeras creaciones de contenedor responden 500
  status_seq: [..]         estados que devuelve el contenedor en cada consulta (luego FINISHED)
  publish_500_after: True  media_publish publica pero responde 500 (simula un corte justo después)
  publish_400: True        media_publish rechaza
  comment_500: n           los n primeros comentarios responden 500
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Estado:
    def __init__(self):
        self.contenedores = {}  # id -> {caption, video_url, consultas}
        self.medios = []  # [{id, caption, timestamp}] (más reciente primero)
        self.comentarios = []  # [(media_id, message)]
        self.llamadas = []  # [(método, ruta)]
        self.fallos = {}
        self.sec = 1000
        self.token = "TEST_TOKEN"

    def nuevo_id(self):
        self.sec += 1
        return str(self.sec)


def crear_servidor():
    estado = Estado()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _resp(self, code, obj=None):
            cuerpo = json.dumps(obj or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cuerpo)

        def _params(self):
            u = urllib.parse.urlparse(self.path)
            p = dict(urllib.parse.parse_qsl(u.query))
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                p.update(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
            return u.path, p

        def do_HEAD(self):
            ruta = urllib.parse.urlparse(self.path).path
            if ruta.startswith("/videos/"):  # imita GitHub Pages
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_GET(self):
            self._api("GET")

        def do_POST(self):
            self._api("POST")

        def _api(self, metodo):
            ruta, p = self._params()
            partes = [x for x in ruta.split("/") if x][1:]  # quita la versión (v21.0)
            estado.llamadas.append((metodo, "/".join(partes)))
            if p.get("access_token") != estado.token:
                return self._resp(401, {"error": {"message": "Invalid OAuth access token"}})
            f = estado.fallos
            if metodo == "GET" and partes == ["17840000000000001"]:
                return self._resp(200, {"id": "17840000000000001", "username": "cuenta_prueba"})
            if metodo == "POST" and len(partes) == 2 and partes[1] == "media":
                if f.get("create_500", 0) > 0:
                    f["create_500"] -= 1
                    return self._resp(500, {"error": {"message": "temporal"}})
                cid = estado.nuevo_id()
                estado.contenedores[cid] = {"caption": p.get("caption", ""), "video_url": p.get("video_url"), "consultas": 0, "publicado": False}
                return self._resp(200, {"id": cid})
            if metodo == "GET" and len(partes) == 2 and partes[1] == "media":
                return self._resp(200, {"data": estado.medios[: int(p.get("limit", 25))]})
            if metodo == "POST" and len(partes) == 2 and partes[1] == "media_publish":
                c = estado.contenedores.get(p.get("creation_id"))
                if not c or f.get("publish_400"):
                    return self._resp(400, {"error": {"message": "invalid creation_id"}})
                if c["publicado"]:
                    return self._resp(400, {"error": {"message": "already published"}})
                c["publicado"] = True
                mid = estado.nuevo_id()
                estado.medios.insert(0, {"id": mid, "caption": c["caption"], "timestamp": "2026-01-01T00:00:00+0000"})
                if f.pop("publish_500_after", False):
                    return self._resp(500, {"error": {"message": "se cortó"}})
                return self._resp(200, {"id": mid})
            if metodo == "POST" and len(partes) == 2 and partes[1] == "comments":
                if f.get("comment_500", 0) > 0:
                    f["comment_500"] -= 1
                    return self._resp(500, {"error": {"message": "temporal"}})
                estado.comentarios.append((partes[0], p.get("message")))
                return self._resp(200, {"id": estado.nuevo_id()})
            if metodo == "GET" and len(partes) == 1 and partes[0] in estado.contenedores:
                c = estado.contenedores[partes[0]]
                seq = f.get("status_seq", [])
                code = seq[c["consultas"]] if c["consultas"] < len(seq) else ("PUBLISHED" if c["publicado"] else "FINISHED")
                c["consultas"] += 1
                return self._resp(200, {"status_code": code, "status": code})
            return self._resp(404, {"error": {"message": f"ruta no simulada: {metodo} {ruta}"}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.estado = estado
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
