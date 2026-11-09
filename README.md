# Parece Inventado · publicador

Historias reales que parecen inventadas. Un Reel por día a las 19:00 (Argentina).

- `schedule.json`: calendario y estado de cada publicación.
- `videos/`: los Reels (servidos por GitHub Pages para que Instagram los pueda descargar).
- `publish.py`: publica con la API oficial de Instagram (graph.instagram.com). `--status`, `--dry-run`, `--force`, `--whoami`.
- `.github/workflows/publish.yml`: corre a las 18:40 y 19:30 de Argentina; si falla abre un Issue.
- `.github/workflows/validate.yml`: tests con Meta simulado, dry-run y chequeo del token en cada cambio.

El comentario inicial se publica solo, pero **no se puede fijar por la API**: fijarlo a mano desde la app.
El token de Instagram dura 60 días: renovarlo antes y actualizar el secreto `IG_ACCESS_TOKEN`.
