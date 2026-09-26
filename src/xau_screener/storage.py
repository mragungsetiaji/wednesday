"""Persistence: saved settings and the M1 bar history.

SQLAlchemy Core keeps this database-agnostic: SQLite by default
(``sqlite:///data/xau.db``), PostgreSQL by pointing ``XAU_DB_URL`` at it
(``postgresql+psycopg://user:pass@host/db`` with ``uv sync --extra postgres``).

Bar times are stored as integer unix seconds of the feed's clock (broker
server time for MT5), the same convention the API uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.engine import make_url

from .timeframes import OHLCV_COLUMNS

DEFAULT_DB_URL = "sqlite:///data/xau.db"

metadata = MetaData()

settings_table = Table(
    "settings",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),  # JSON
)

bars_table = Table(
    "m1_bars",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("symbol", String(64), primary_key=True),
    Column("time", BigInteger, primary_key=True),  # unix seconds, bar open
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Float, nullable=False),
)


alerts_table = Table(
    "alert_log",
    metadata,
    Column("key", String(255), primary_key=True),  # one row per order block per source/symbol
    Column("source", String(32), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("entry", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("sent_at", String(40), nullable=False),  # ISO UTC
    Column("status", String(16), nullable=False),  # "sent" or "failed"
    Column("error", Text, nullable=True),
)


class Store:
    def __init__(self, url: str = DEFAULT_DB_URL):
        self.url = make_url(url)
        if self.url.get_backend_name() == "sqlite" and self.url.database not in (None, "", ":memory:"):
            Path(self.url.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(self.url, future=True)
        if self.backend == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                # WAL lets the API read while the scan thread writes.
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        metadata.create_all(self.engine)

    @property
    def backend(self) -> str:
        return self.url.get_backend_name()

    def describe(self) -> dict:
        """Backend and URL without the password, for the settings page."""
        return {"backend": self.backend, "url": self.url.render_as_string(hide_password=True)}

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(settings_table.c.value).where(settings_table.c.key == key)).first()
        return json.loads(row[0]) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        self._upsert(settings_table, [{"key": key, "value": json.dumps(value)}], ["key"])

    def delete_setting(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(settings_table.delete().where(settings_table.c.key == key))

    # ---- bars -----------------------------------------------------------
    def save_bars(self, source: str, symbol: str, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        # Explicit unit: pandas may hold ns or us resolution depending on how the index was built.
        times = pd.DatetimeIndex(df.index).as_unit("s").asi8.tolist()
        rows = [
            {"source": source, "symbol": symbol, "time": int(t), **{c: float(v) for c, v in zip(OHLCV_COLUMNS, vals)}}
            for t, vals in zip(times, df[OHLCV_COLUMNS].itertuples(index=False, name=None))
        ]
        for i in range(0, len(rows), 5000):
            self._upsert(bars_table, rows[i : i + 5000], ["source", "symbol", "time"])
        return len(rows)

    def load_bars(self, source: str, symbol: str, limit: int) -> pd.DataFrame:
        t = bars_table
        q = (
            select(t.c.time, t.c.open, t.c.high, t.c.low, t.c.close, t.c.volume)
            .where(t.c.source == source, t.c.symbol == symbol)
            .order_by(t.c.time.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        if not rows:
            return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([]), dtype=float)
        df = pd.DataFrame(rows, columns=["time", *OHLCV_COLUMNS])
        df.index = pd.to_datetime(df.pop("time"), unit="s")
        df.index.name = None
        return df.sort_index().astype(float)

    def bar_stats(self) -> list[dict]:
        t = bars_table
        q = select(t.c.source, t.c.symbol, func.count(), func.min(t.c.time), func.max(t.c.time)).group_by(t.c.source, t.c.symbol)
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()

        def iso(ts):
            return pd.Timestamp(ts, unit="s").isoformat() if ts is not None else None

        return [{"source": s, "symbol": sym, "bars": n, "first": iso(a), "last": iso(b)} for s, sym, n, a, b in rows]

    # ---- alerts ---------------------------------------------------------
    def alert_sent(self, key: str) -> bool:
        t = alerts_table
        with self.engine.connect() as conn:
            row = conn.execute(select(t.c.status).where(t.c.key == key)).first()
        return bool(row and row[0] == "sent")

    def log_alert(self, row: dict) -> None:
        self._upsert(alerts_table, [row], ["key"])

    def recent_alerts(self, limit: int = 20) -> list[dict]:
        t = alerts_table
        q = select(t).order_by(t.c.sent_at.desc()).limit(limit)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    # ---- helpers --------------------------------------------------------
    def _upsert(self, table: Table, rows: list[dict], keys: list[str]) -> None:
        if not rows:
            return
        if self.backend == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif self.backend == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:  # pragma: no cover - other databases: delete then insert
            with self.engine.begin() as conn:
                for r in rows:
                    conn.execute(table.delete().where(*[table.c[k] == r[k] for k in keys]))
                conn.execute(table.insert(), rows)
            return
        stmt = insert(table)
        update = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in keys}
        with self.engine.begin() as conn:
            conn.execute(stmt.on_conflict_do_update(index_elements=keys, set_=update), rows)
