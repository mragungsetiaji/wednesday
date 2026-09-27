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
    Boolean,
    Column,
    Float,
    Index,
    Integer,
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


# ---- Lab (machine learning): hand-made labels, reviewed ranges, reviews of model output ----
# Times are unix seconds of the feed's clock, like the bars.

lab_labels_table = Table(
    "lab_labels",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("symbol", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("tag", String(32), nullable=False),
    Column("value", Integer, nullable=False),  # 1 = it is one, 0 = explicitly not one
    Column("start", BigInteger, nullable=False),  # open time of the first candle
    Column("end", BigInteger, nullable=False),  # open time of the last candle
    Column("top", Float, nullable=True),
    Column("bottom", Float, nullable=True),
    Column("origin", String(16), nullable=False),  # "manual", "detector" (accepted suggestion) or "review"
    Column("created_at", String(40), nullable=False),
)

lab_reviewed_table = Table(
    "lab_reviewed",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("symbol", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("start", BigInteger, nullable=False),
    Column("end", BigInteger, nullable=False),
    Column("tags", Text, nullable=False),  # JSON list: tags fully labelled in this range
    Column("created_at", String(40), nullable=False),
)

lab_reviews_table = Table(
    "lab_reviews",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("symbol", String(64), nullable=False),
    Column("model_id", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("tag", String(32), nullable=False),
    Column("time", BigInteger, nullable=False),  # open time of the candle the model flagged
    Column("prob", Float, nullable=False),
    Column("verdict", String(8), nullable=False),  # "valid" or "invalid"
    Column("outcome", String(16), nullable=True),  # "win", "loss", "open", "untouched" (order blocks)
    Column("r", Float, nullable=True),  # result in R when the trade finished
    Column("created_at", String(40), nullable=False),
)


# ---- Journal: trading accounts imported from MT5 --------------------------------------------
# Deal times are unix seconds of the broker's server clock, as MT5 reports them.

journals_table = Table(
    "journals",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("login", String(32), nullable=True),  # the MT5 account number (never a password)
    Column("server", String(120), nullable=True),
    Column("company", String(120), nullable=True),
    Column("currency", String(8), nullable=True),
    Column("source", String(16), nullable=True),  # how it was last filled: "mt5" or "report"
    Column("account", Text, nullable=True),  # JSON: balance/equity the terminal reported at the last sync
    Column("time_offset", Integer, nullable=True),  # seconds from deal clock to price clock; null = detect
    Column("created_at", String(40), nullable=False),
    Column("synced_at", String(40), nullable=True),
)

journal_trades_table = Table(
    "journal_trades",
    metadata,
    Column("journal_id", String(36), primary_key=True),
    Column("id", String(64), primary_key=True),  # "p<position>" or "p<position>:<closing deal>"
    Column("position", String(32), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("side", String(4), nullable=False),  # "buy" or "sell"
    Column("volume", Float, nullable=False),  # lots
    Column("open_time", BigInteger, nullable=False),
    Column("open_price", Float, nullable=False),
    Column("close_time", BigInteger, nullable=True),  # null while open
    Column("close_price", Float, nullable=True),  # the current price while open
    Column("profit", Float, nullable=False),  # price P/L only, in the account currency
    Column("commission", Float, nullable=False),  # commission and fees
    Column("swap", Float, nullable=False),
    Column("sl", Float, nullable=True),
    Column("tp", Float, nullable=True),
    Column("comment", Text, nullable=True),
)

journal_cash_table = Table(
    "journal_cash",
    metadata,
    Column("journal_id", String(36), primary_key=True),
    Column("id", String(64), primary_key=True),  # the MT5 deal ticket
    Column("time", BigInteger, nullable=False),
    Column("kind", String(16), nullable=False),  # "deposit", "withdrawal", "credit" or "other"
    Column("amount", Float, nullable=False),
    Column("comment", Text, nullable=True),
)

journal_notes_table = Table(
    "journal_notes",
    metadata,
    Column("journal_id", String(36), primary_key=True),
    Column("trade_id", String(64), primary_key=True),
    Column("note", Text, nullable=False),
    Column("tags", Text, nullable=False),  # JSON list
    Column("updated_at", String(40), nullable=False),
)


# ---- Chart drawings: trendlines, horizontal lines, rectangles, ... ---------------------------
# Anchored to (time, price), not pixels, so one drawing shows on every timeframe. Kept per
# source and symbol: MT5 times are the broker's clock and Yahoo's GC=F isn't spot XAUUSD.

DRAWING_KINDS = ("trendline", "hline", "rect", "long", "short", "path", "text")

drawings_table = Table(
    "drawings",
    metadata,
    Column("id", String(36), primary_key=True),  # uuid made by the browser
    Column("source", String(32), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("points", Text, nullable=False),  # JSON [{"t": unix seconds (feed clock), "p": price}, ...]
    Column("style", Text, nullable=False),  # JSON {"color": null = theme default, "width": 1, ...}
    Column("props", Text, nullable=True),  # JSON, per kind (e.g. a position's entry, stop, target)
    Column("timeframes", Text, nullable=True),  # JSON list the drawing shows on; null = every timeframe
    Column("locked", Boolean, nullable=False, default=False),
    Column("hidden", Boolean, nullable=False, default=False),
    Column("updated_at", String(40), nullable=False),
    Index("ix_drawings_source_symbol", "source", "symbol"),
)
_DRAWING_JSON = ("points", "style", "props", "timeframes")


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

    def load_bars(self, source: str, symbol: str, limit: int, before: int | None = None) -> pd.DataFrame:
        """The latest ``limit`` stored M1 bars, or the latest opening before ``before`` (unix seconds)."""
        t = bars_table
        q = select(t.c.time, t.c.open, t.c.high, t.c.low, t.c.close, t.c.volume).where(
            t.c.source == source, t.c.symbol == symbol)
        if before is not None:
            q = q.where(t.c.time < before)
        q = q.order_by(t.c.time.desc()).limit(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        if not rows:
            return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([]), dtype=float)
        df = pd.DataFrame(rows, columns=["time", *OHLCV_COLUMNS])
        df.index = pd.to_datetime(df.pop("time"), unit="s")
        df.index.name = None
        return df.sort_index().astype(float)

    def load_bar_range(self, source: str, symbol: str, start: int, end: int) -> pd.DataFrame:
        """Stored M1 bars with open times in [start, end] (unix seconds of the feed clock)."""
        t = bars_table
        q = (
            select(t.c.time, t.c.open, t.c.high, t.c.low, t.c.close, t.c.volume)
            .where(t.c.source == source, t.c.symbol == symbol, t.c.time >= start, t.c.time <= end)
            .order_by(t.c.time)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        df = pd.DataFrame(rows, columns=["time", *OHLCV_COLUMNS])
        df.index = pd.to_datetime(df.pop("time"), unit="s")
        df.index.name = None
        return df.astype(float)

    def bar_bounds(self, source: str, symbol: str) -> tuple[int, int | None, int | None]:
        """Stored M1 bars of one stream: count, first and last open time (unix seconds)."""
        t = bars_table
        q = select(func.count(), func.min(t.c.time), func.max(t.c.time)).where(t.c.source == source, t.c.symbol == symbol)
        with self.engine.connect() as conn:
            n, first, last = conn.execute(q).one()
        return int(n), first, last

    def bar_days(self, source: str, symbol: str) -> list[tuple[int, int]]:
        """Stored M1 bars per day: (day as unix seconds of its midnight, bar count), oldest first."""
        t = bars_table
        day = (t.c.time // 86400).label("day")
        q = (select(day, func.count()).where(t.c.source == source, t.c.symbol == symbol)
             .group_by(day).order_by(day))
        with self.engine.connect() as conn:
            return [(int(d) * 86400, int(n)) for d, n in conn.execute(q).all()]

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

    # ---- lab ------------------------------------------------------------
    def lab_rows(self, table: Table, symbol: str, timeframe: str | None = None,
                 start: int | None = None, end: int | None = None) -> list[dict]:
        """Rows of a lab table for a symbol, optionally one timeframe and overlapping [start, end]."""
        t = table
        cond = [t.c.symbol == symbol]
        if timeframe:
            cond.append(t.c.timeframe == timeframe)
        time_start, time_end = (t.c.time, t.c.time) if "time" in t.c else (t.c.start, t.c.end)
        if end is not None:
            cond.append(time_start <= end)
        if start is not None:
            cond.append(time_end >= start)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(t).where(*cond).order_by(time_start))]
        if table is lab_reviewed_table:
            for r in rows:
                r["tags"] = json.loads(r["tags"])
        return rows

    def lab_put(self, table: Table, row: dict) -> None:
        if table is lab_reviewed_table:
            row = {**row, "tags": json.dumps(row["tags"])}
        self._upsert(table, [row], ["id"])

    def lab_delete(self, table: Table, row_id: str) -> bool:
        with self.engine.begin() as conn:
            return conn.execute(table.delete().where(table.c.id == row_id)).rowcount > 0

    # ---- drawings -------------------------------------------------------
    def drawings(self, source: str, symbol: str) -> list[dict]:
        t = drawings_table
        q = select(t).where(t.c.source == source, t.c.symbol == symbol).order_by(t.c.updated_at)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(q)]
        for r in rows:
            for k in _DRAWING_JSON:
                r[k] = json.loads(r[k]) if r[k] is not None else None
        return rows

    def drawing_put(self, row: dict) -> None:
        row = {**row, **{k: json.dumps(row[k]) if row.get(k) is not None else None for k in _DRAWING_JSON}}
        self._upsert(drawings_table, [row], ["id"])

    def drawing_delete(self, drawing_id: str, source: str, symbol: str) -> bool:
        t = drawings_table
        with self.engine.begin() as conn:
            cond = (t.c.id == drawing_id, t.c.source == source, t.c.symbol == symbol)
            return conn.execute(t.delete().where(*cond)).rowcount > 0

    # ---- journal --------------------------------------------------------
    def journals(self) -> list[dict]:
        t = journals_table
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(t).order_by(t.c.created_at))]
        for r in rows:
            r["account"] = json.loads(r["account"]) if r["account"] else None
        return rows

    def journal_put(self, row: dict) -> None:
        row = {**row, "account": json.dumps(row["account"]) if row.get("account") is not None else None}
        self._upsert(journals_table, [row], ["id"])

    def journal_delete(self, journal_id: str) -> bool:
        with self.engine.begin() as conn:
            for t in (journal_trades_table, journal_cash_table, journal_notes_table):
                conn.execute(t.delete().where(t.c.journal_id == journal_id))
            return conn.execute(journals_table.delete().where(journals_table.c.id == journal_id)).rowcount > 0

    def journal_rows(self, table: Table, journal_id: str) -> list[dict]:
        """Trades, cash or notes of a journal, oldest first."""
        t = table
        q = select(t).where(t.c.journal_id == journal_id)
        if "open_time" in t.c or "time" in t.c:
            q = q.order_by(t.c.open_time if "open_time" in t.c else t.c.time)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(q)]
        if table is journal_notes_table:
            for r in rows:
                r["tags"] = json.loads(r["tags"])
        return rows

    def journal_fill(self, journal_id: str, trades: list[dict], cash: list[dict], replace: bool) -> None:
        """Save imported trades and cash. ``replace`` drops what was there first (a full sync)."""
        if replace:
            with self.engine.begin() as conn:
                for t in (journal_trades_table, journal_cash_table):
                    conn.execute(t.delete().where(t.c.journal_id == journal_id))
        for table, rows in ((journal_trades_table, trades), (journal_cash_table, cash)):
            rows = [{**r, "journal_id": journal_id} for r in rows]
            for i in range(0, len(rows), 2000):
                self._upsert(table, rows[i : i + 2000], ["journal_id", "id"])

    def journal_note(self, journal_id: str, trade_id: str, note: str, tags: list[str], updated_at: str) -> None:
        if not note and not tags:
            with self.engine.begin() as conn:
                t = journal_notes_table
                conn.execute(t.delete().where(t.c.journal_id == journal_id, t.c.trade_id == trade_id))
            return
        self._upsert(journal_notes_table, [{"journal_id": journal_id, "trade_id": trade_id, "note": note,
                                            "tags": json.dumps(tags), "updated_at": updated_at}],
                     ["journal_id", "trade_id"])

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
