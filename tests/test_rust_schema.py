"""The database schema the Rust server creates (#96): generated from ``storage.metadata``.

``wed-store/schema.json`` holds, per table, the ``CREATE TABLE`` and ``CREATE INDEX`` statements
SQLAlchemy issues on SQLite and on PostgreSQL, and each column's type and nullability (for adding
columns a newer version introduced to an older database). The Rust store runs exactly these, so
both servers make and read the same tables. After a change to the tables in ``storage.py``:

    UPDATE_RUST_SCHEMA=1 uv run pytest tests/test_rust_schema.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from wednesday.storage import metadata

SCHEMA = Path(__file__).parents[1] / "wed-store" / "schema.json"
DIALECTS = {"sqlite": sqlite.dialect(), "postgresql": postgresql.dialect()}


def _sql(stmt, dialect) -> str:
    return " ".join(str(stmt.compile(dialect=dialect)).split())


def schema() -> dict:
    tables = []
    for table in metadata.sorted_tables:
        tables.append({
            "name": table.name,
            "create": {d: _sql(CreateTable(table, if_not_exists=True), dia) for d, dia in DIALECTS.items()},
            "indexes": {d: [_sql(CreateIndex(ix, if_not_exists=True), dia)
                            for ix in sorted(table.indexes, key=lambda ix: ix.name)] for d, dia in DIALECTS.items()},
            "columns": [{"name": c.name, "nullable": bool(c.nullable),
                         **{d: c.type.compile(dialect=dia) for d, dia in DIALECTS.items()}} for c in table.columns],
        })
    return {"tables": tables}


def test_the_rust_schema_matches_storage():
    now = schema()
    if os.environ.get("UPDATE_RUST_SCHEMA") or not SCHEMA.exists():
        SCHEMA.write_text(json.dumps(now, indent=1) + "\n", encoding="utf-8")
        return
    assert json.loads(SCHEMA.read_text(encoding="utf-8")) == now, (
        "storage.py's tables changed: run UPDATE_RUST_SCHEMA=1 uv run pytest tests/test_rust_schema.py "
        "and make the Rust store (wed-store/src/lib.rs) handle the new columns")
