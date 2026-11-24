# ArchGate Web (prototype)

A small web UI for ArchGate. You fill in requirements in a form, and it shows the generated
design with its diagram, components, configuration decisions and reasoning.

## What's different from the CLI

| | Web | CLI |
|---|---|---|
| Rules engine, estimator, validator | Yes | Yes |
| Local open model (Ollama) | No, rules only | Yes, optional |
| Plain-English input | No | Yes, with a model |
| Approval gate (approve/reject) | No | Yes |
| Login | No | Not needed |
| Download diagram (SVG, PNG, Mermaid) | Yes | `.mmd` file in the run folder |
| Download design doc / run JSON | Yes | Files in the run folder |

The web version is a prototype for trying designs quickly. Use the CLI when you need the model
or the approval gate.

## Run it locally

```bash
uv sync --extra web
uv run archgate serve
```

Open http://127.0.0.1:8000. Designs are stored in `archgate.db` (SQLite) in the current folder.

Options:

```bash
uv run archgate serve --port 9000
uv run archgate serve --database-url postgresql://user:pass@localhost/archgate
```

## Pages

- **Designs** (`/`): all generated designs, newest first, with their confidence.
- **New design** (`/new`): the requirements form. The basics are required, and "More options"
  holds the optional fields (regions, RPO/RTO, compliance, data model and so on). You can also
  paste requirements YAML instead of filling in the form.
- **Design** (`/runs/<id>`):
  - Architecture diagram with **Download SVG**, **Download PNG** and **Download Mermaid**
  - Capacity estimate
  - Components, with the rule that chose each one and why
  - Configuration decisions (shard key, cache strategy, backups and so on)
  - Considered and rejected components
  - Requirements used, and the full decision trace
  - **Download design doc** (Markdown) and **Download run (JSON)**

## Deploy to Render

The repo includes `render.yaml`, a Render Blueprint for a free web service.

1. Push the repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo, and confirm.
3. When the build finishes, open the service URL.

Or set it up by hand as a **Web Service** with:

| Setting | Value |
|---|---|
| Runtime | Python |
| Build command | `pip install ".[web]"` |
| Start command | `uvicorn archgate.web.app:create_app --factory --host 0.0.0.0 --port $PORT` |
| Health check path | `/healthz` |
| Environment | `PYTHON_VERSION=3.12.7` |

Things to know about Render:

- **Designs are temporary by default.** Without `DATABASE_URL`, designs live in SQLite on the
  instance's disk, which Render wipes on every deploy or restart. To keep them, create a Postgres
  database (on Render or elsewhere) and set `DATABASE_URL` to its connection string; the
  `postgres://` URL Render gives you works as-is.
- **Free instances sleep** after a period of no traffic, so the first request afterwards is slow.
- **The site is public.** There's no login, so anyone with the URL can create and view designs.
  That's fine for a prototype; don't put sensitive requirements into it.

Check Render's docs and pricing for current plan limits.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///archgate.db` | Where designs are stored (SQLite or Postgres) |
| `ARCHGATE_LEARNED_RULES` | unset | Path to a learned-rules file to load alongside the bundled rules |
| `PORT` | set by Render | Port to listen on (used in the start command) |

## Code layout

```
src/archgate/web/
  app.py        FastAPI routes; builds a rules-only Pipeline once at startup
  forms.py      form fields / pasted YAML -> requirements
  storage.py    runs table (SQLAlchemy): SQLite or Postgres
  templates/    base, index (list), new (form), run (design view + downloads)
```

The diagram is drawn in the browser with Mermaid. SVG and PNG downloads are made from the
rendered diagram, so they match exactly what you see.
