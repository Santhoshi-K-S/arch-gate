"""Rules-only web UI. Run locally with: archgate serve"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from archgate import __version__
from archgate.errors import RequirementsError
from archgate.knowledge import load_knowledge_base
from archgate.pipeline import Pipeline, Run
from archgate.render import markdown, mermaid, rejected_alternatives
from archgate.requirements import (
    BUDGET_LEVELS,
    COMPLIANCE_REGIMES,
    CONSISTENCY_LEVELS,
    DATA_MODELS,
    ORDERING_LEVELS,
    TEAM_SIZES,
    TRAFFIC_PATTERNS,
    WRITE_PATTERNS,
    Requirements,
)
from archgate.store import to_dict
from archgate.web import forms
from archgate.web.storage import RunStore

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
CHOICES = {
    "consistency": CONSISTENCY_LEVELS,
    "budget": BUDGET_LEVELS,
    "data_model": DATA_MODELS,
    "write_pattern": WRITE_PATTERNS,
    "traffic_pattern": TRAFFIC_PATTERNS,
    "ordering": ORDERING_LEVELS,
    "team_size": TEAM_SIZES,
}
KNOWN_FEATURES = (
    "chat, realtime, collaboration, feed, media, file_upload, search, semantic_search, "
    "recommendations, payments, geo, notifications, analytics, batch_jobs, public_api, short_links"
)


@dataclass(frozen=True)
class WebConfig:
    database_url: str = "sqlite:///archgate.db"
    learned_rules: Path | None = None

    @classmethod
    def from_env(cls) -> WebConfig:
        learned = os.environ.get("ARCHGATE_LEARNED_RULES")
        return cls(
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            learned_rules=Path(learned) if learned else None,
        )


def create_app(config: WebConfig | None = None) -> FastAPI:
    config = config or WebConfig.from_env()
    kb = load_knowledge_base(learned_path=config.learned_rules)
    # no model on the web: designs are deterministic and free to run
    pipeline = Pipeline(kb)
    store = RunStore(config.database_url)
    catalog = kb.catalog

    app = FastAPI(title="ArchGate", version=__version__, docs_url=None, redoc_url=None)

    def page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        context.update(version=__version__, kb_version=kb.version, rule_count=len(kb.rules))
        return TEMPLATES.TemplateResponse(request, name, context, status_code=status_code)

    def load(run_id: str) -> Run:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "design not found")
        return run

    def download(body: str, filename: str, media_type: str) -> Response:
        return Response(body, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return page(request, "index.html", runs=store.recent())

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request):
        return page(request, "new.html", values={}, errors=[], choices=CHOICES,
                    regimes=COMPLIANCE_REGIMES, known_features=KNOWN_FEATURES)

    @app.post("/designs")
    async def create(request: Request):
        form = await request.form()
        values = dict(form)
        compliance = form.getlist("compliance")
        try:
            yaml_text = str(values.get("yaml", "")).strip()
            data = forms.from_yaml(yaml_text) if yaml_text else forms.from_form(values, compliance)
            req = Requirements.from_dict(data)
        except RequirementsError as exc:
            return page(request, "new.html", status_code=400, values=values, errors=exc.problems,
                        choices=CHOICES, regimes=COMPLIANCE_REGIMES,
                        known_features=KNOWN_FEATURES)
        run = pipeline.run(req)
        store.add(run, catalog)
        return RedirectResponse(f"/runs/{run.run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def view(request: Request, run_id: str):
        run = load(run_id)
        rejected, blocked = rejected_alternatives(run)
        components = [
            (cid, catalog.components[cid] if cid in catalog else None, run.design.choices[cid])
            for cid in catalog.order(run.design.components)
        ]
        return page(request, "run.html", run=run, components=components,
                    diagram=mermaid(run.design, catalog), rejected=rejected, blocked=blocked)

    @app.get("/runs/{run_id}/diagram.mmd")
    def diagram(run_id: str):
        run = load(run_id)
        return download(mermaid(run.design, catalog), f"{run_id}.mmd", "text/plain")

    @app.get("/runs/{run_id}/design.md")
    def design_doc(run_id: str):
        return download(markdown(load(run_id), catalog), f"{run_id}.md", "text/markdown")

    @app.get("/runs/{run_id}/run.json")
    def run_json(run_id: str):
        data = json.dumps(to_dict(load(run_id), catalog), indent=2)
        return download(data, f"{run_id}.json", "application/json")

    return app
