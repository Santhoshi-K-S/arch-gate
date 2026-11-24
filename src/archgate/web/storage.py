"""Runs in a database: SQLite locally, Postgres on Render."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, insert, select

from archgate.knowledge import Catalog
from archgate.pipeline import Run
from archgate.store import from_dict, to_dict

metadata = MetaData()

runs_table = Table(
    "runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("created_at", String(40), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("state", String(32), nullable=False),
    Column("confidence", String(32)),
    Column("data", Text, nullable=False),
)


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    created_at: str
    name: str
    state: str
    confidence: str | None


def normalize_url(url: str) -> str:
    # Render hands out postgres:// URLs; SQLAlchemy needs the driver named
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class RunStore:
    def __init__(self, url: str):
        self.engine = create_engine(normalize_url(url), pool_pre_ping=True)
        metadata.create_all(self.engine)

    def add(self, run: Run, catalog: Catalog) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(runs_table).values(
                run_id=run.run_id,
                created_at=run.created_at,
                name=run.requirements.name if run.requirements else run.run_id,
                state=run.state.value,
                confidence=run.confidence,
                data=json.dumps(to_dict(run, catalog)),
            ))

    def get(self, run_id: str) -> Run | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(runs_table.c.data).where(runs_table.c.run_id == run_id)
            ).first()
        return from_dict(json.loads(row.data)) if row else None

    def recent(self, limit: int = 100) -> list[RunSummary]:
        cols = runs_table.c
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(cols.run_id, cols.created_at, cols.name, cols.state, cols.confidence)
                .order_by(cols.created_at.desc())
                .limit(limit)
            ).all()
        return [RunSummary(*row) for row in rows]
