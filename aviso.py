#!/usr/bin/env python3
"""Prepara el mail del día: descripción y comentario del Reel publicado HOY, para copiarlos desde el celular.

    python aviso.py   → si hay un Reel publicado hoy sin aviso, escribe aviso_titulo.txt y aviso_cuerpo.md y marca
                        aviso_enviado en schedule.json. El workflow abre (y cierra) un Issue con ese texto: GitHub
                        se lo manda por mail al dueño del repositorio.
"""
import sys

import publish


def main():
    cal = publish.cargar()
    hoy = publish.ahora().date()
    d = next((x for x in cal["days"] if x.get("published_at") and publish.fecha_de(x["published_at"]) == hoy and not x.get("aviso_enviado")), None)
    if d is None:
        print("No hay aviso para mandar.")
        return 0
    titulo = d["caption"].split("\n", 1)[0]
    tiktok = {
        "sent": "📱 **TikTok:** ya te llegó el borrador. Abrí TikTok → Bandeja de entrada → Notificaciones del sistema → editá el video, pegá la descripción y publicá.",
        "failed": "⚠️ **TikTok:** el borrador no se pudo mandar. Subí el video a mano con la descripción de abajo.",
    }.get(d.get("tiktok_status"), "")
    cuerpo = f"""Ya salió **{titulo}** en Instagram (19 h).

📌 **Instagram:** abrí el Reel, mantené apretado tu comentario y tocá **Fijar**.
{tiktok}

### Descripción (mantené apretado para copiar)

```
{d['caption']}
```

### Comentario para fijar

```
{d.get('first_comment', '')}
```
"""
    raiz = publish.ruta_calendario().parent
    (raiz / "aviso_titulo.txt").write_text(f"Hoy: {titulo}", encoding="utf-8")
    (raiz / "aviso_cuerpo.md").write_text(cuerpo, encoding="utf-8")
    d["aviso_enviado"] = True
    publish.guardar(cal)
    print(f"Aviso preparado: {titulo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
