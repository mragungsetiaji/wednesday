"""More M1 history: import M1 files into the stored bars (``/api/history``).

Older bars give the chart more history to scroll back through and the journal's
drawdown more M1 prices. Imports write into the stored bars of the running source and symbol, as an upsert
on time, so importing a file twice or overlapping files adds nothing twice.

File formats recognised:

* **MT5 export** (History Center / chart export): ``<DATE> <TIME> <OPEN> ...``,
  tab separated, dates like ``2024.01.02``. Times are the broker's server time.
* **Dukascopy**: ``Gmt time,Open,High,Low,Close,Volume`` with ``02.01.2024 00:00:00.000``. UTC.
* **HistData** ASCII M1: no header, ``20240102 180000;open;high;low;close;volume``,
  or the MetaStock style ``2024.01.02,18:00,open,...``. Fixed UTC-5 (EST, no DST).
* **Any CSV** with a ``time`` / ``datetime`` / ``date`` column and open, high, low, close.
"""

from __future__ import annotations

import io
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from fastapi import APIRouter, Body, HTTPException, Request

from .quarters import clock_error, convert_clock
from .timeframes import OHLCV_COLUMNS, normalize_ohlcv

if TYPE_CHECKING:
    from .engine import Engine
    from .storage import Store

log = logging.getLogger(__name__)

MAX_IMPORT_BYTES = 500 * 1024 * 1024
DEMO_DATA = "Demo data isn't stored: switch to TradingView, Yahoo Finance or CSV first"
NO_DATABASE = "Importing history needs a database: start the server without --db none"


class BarsFileError(ValueError):
    """The file isn't M1 bars in a format we read."""


@dataclass
class ParsedBars:
    bars: pd.DataFrame  # naive times in the file's own clock, oldest first
    format: str  # "mt5", "dukascopy", "histdata", "csv"
    clock: str | None  # the format's usual clock; None when it is the broker's (MT5 export)

    def preview(self) -> dict:
        b = self.bars
        return {"format": self.format, "suggested_clock": self.clock, "bars": len(b),
                "first": b.index[0].isoformat(), "last": b.index[-1].isoformat(),
                "sample": [{"time": t.isoformat(), **{c: float(r[c]) for c in OHLCV_COLUMNS}}
                           for t, r in b.head(5).iterrows()]}


def _text(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")  # MT5 writes UTF-16 files
    return data.decode("utf-8-sig", errors="replace")


def _delimiter(line: str) -> str:
    return max(("\t", ";", ","), key=line.count)


def parse_bars(data: bytes) -> ParsedBars:
    """Read an M1 file; raises :class:`BarsFileError` with a reason the trader can act on."""
    if not data.strip():
        raise BarsFileError("The file is empty")
    text = _text(data)
    first = text.lstrip().split("\n", 1)[0]
    sep = _delimiter(first)
    has_header = bool(re.search(r"[A-Za-z]", first.split(sep)[0]))
    try:
        raw = pd.read_csv(io.StringIO(text), sep=sep, header=0 if has_header else None, dtype=str,
                          skipinitialspace=True)
    except (pd.errors.ParserError, UnicodeError) as exc:
        raise BarsFileError(f"Can't read the file as CSV: {exc}") from exc
    if raw.empty:
        raise BarsFileError("The file has no rows")

    if has_header:
        raw.columns = [str(c).strip().strip("<>").lower() for c in raw.columns]
        cols = set(raw.columns)
        if {"date", "time"} <= cols:
            fmt, clock = "mt5", None
            times = _to_time(raw.pop("date") + " " + raw.pop("time"))
            if "tickvol" in raw.columns:
                raw = raw.drop(columns=["volume", "vol"], errors="ignore").rename(columns={"tickvol": "volume"})
        elif "gmt time" in cols:
            fmt, clock = "dukascopy", "UTC"
            times = _to_time(raw.pop("gmt time"), fmt="%d.%m.%Y %H:%M:%S.%f", dayfirst=True)
        else:
            col = next((c for c in ("time", "datetime", "timestamp", "date", "local time") if c in cols), None)
            if col is None:
                raise BarsFileError("No time column: expected one named time, datetime, date or timestamp")
            fmt, clock = "csv", "UTC"
            times = _to_time(raw.pop(col))
    else:
        width = raw.shape[1]
        if width >= 6 and re.fullmatch(r"\d{4}[.\-/]?\d{2}[.\-/]?\d{2}", str(raw.iloc[0, 0]).strip()) \
                and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", str(raw.iloc[0, 1]).strip()):
            times = _to_time(raw.iloc[:, 0] + " " + raw.iloc[:, 1])
            raw = raw.iloc[:, 2:]
        elif width >= 5 and re.fullmatch(r"\d{8} \d{6}", str(raw.iloc[0, 0]).strip()):
            times = _to_time(raw.iloc[:, 0], fmt="%Y%m%d %H%M%S")
            raw = raw.iloc[:, 1:]
        else:
            raise BarsFileError("Unknown layout: add a header row with time, open, high, low, close")
        fmt, clock = "histdata", "UTC-5"
        raw = raw.iloc[:, :5]
        raw.columns = OHLCV_COLUMNS[: raw.shape[1]]

    raw.index = times
    raw = raw[raw.index.notna()]
    missing = [c for c in ("open", "high", "low", "close") if c not in raw.columns]
    if missing:
        raise BarsFileError(f"Missing columns: {', '.join(missing)}")
    for c in OHLCV_COLUMNS:
        raw[c] = pd.to_numeric(raw[c], errors="coerce") if c in raw.columns else 0.0
    bars = normalize_ohlcv(raw.dropna(subset=["open", "high", "low", "close"]))
    bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    if len(bars) < 2:
        raise BarsFileError("Fewer than two bars in the file")
    step = pd.Series(bars.index).diff().median()
    if step != pd.Timedelta(minutes=1):
        raise BarsFileError(f"These aren't 1-minute bars (the usual gap is {step}); export M1")
    return ParsedBars(bars, fmt, clock)


def _to_time(values: pd.Series, fmt: str | None = None, dayfirst: bool = False) -> pd.DatetimeIndex:
    """Times from text: the expected layout first (fast), any layout pandas knows if that misses."""
    values = values.astype(str).str.strip()
    if fmt is None:
        values = values.str.replace(r"^(\d{4})\.(\d{2})\.(\d{2})", r"\1-\2-\3", regex=True)  # 2024.01.02
    out = None
    try:
        out = pd.to_datetime(values, format=fmt or "ISO8601", errors="coerce")
    except (ValueError, TypeError):
        pass
    if out is None or out.isna().mean() > 0.01:
        try:
            out = pd.to_datetime(values, format="mixed", dayfirst=dayfirst, errors="coerce")
        except (ValueError, TypeError) as exc:
            raise BarsFileError(f"Can't read the times: {exc}") from exc
    idx = pd.DatetimeIndex(out)
    if idx.tz is not None:  # an offset in the file: make it UTC wall time
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx


def import_bars(store: Store, key: tuple[str, str], parsed: ParsedBars, file_clock: str, feed_clock: str) -> dict:
    """Store the file's bars under ``key``, moved from ``file_clock`` to the feed's clock."""
    if err := clock_error(file_clock):
        raise BarsFileError(err)
    bars = parsed.bars.copy()
    bars.index = convert_clock(bars.index, file_clock, feed_clock)
    bars = bars[bars.index.notna()]
    bars = bars[~bars.index.duplicated(keep="first")]
    before, _, _ = store.bar_bounds(*key)
    store.save_bars(*key, bars)
    after, _, _ = store.bar_bounds(*key)
    return {"read": len(bars), "added": after - before, "first": bars.index[0].isoformat(),
            "last": bars.index[-1].isoformat(), "stored": after}


class StagedImports:
    """Uploaded files waiting for the trader to confirm them, by token (the last two only)."""

    KEEP = 2

    def __init__(self) -> None:
        self._files: dict[str, ParsedBars] = {}

    def stage(self, data: bytes) -> dict:
        """Read an M1 file and keep it until :meth:`take`; returns the preview."""
        parsed = parse_bars(data)
        token = secrets.token_hex(8)
        while len(self._files) >= self.KEEP:
            self._files.pop(next(iter(self._files)))
        self._files[token] = parsed
        return {"token": token, **parsed.preview()}

    def take(self, token: str) -> ParsedBars:
        parsed = self._files.pop(token, None)
        if parsed is None:
            raise BarsFileError("That upload expired; choose the file again")
        return parsed


def history_router(get_engine: Callable[[], Engine], get_source: Callable[[], str],
                   get_clock: Callable[[], str] = lambda: "UTC") -> APIRouter:
    """``/api/history``: stored M1 coverage per day, and importing M1 files into it."""
    r = APIRouter(prefix="/api/history")
    staged = StagedImports()

    def stream() -> tuple[Store, tuple[str, str]] | None:
        """The store and key the running source's bars go under; None for demo data or no database."""
        buffer = get_engine().buffer
        return (buffer.store, buffer.key) if buffer.store is not None and buffer.key is not None else None

    def need_stream() -> tuple[Store, tuple[str, str]]:
        found = stream()
        if found is None:
            raise HTTPException(409, DEMO_DATA if get_engine().buffer.key is None else NO_DATABASE)
        return found

    def status() -> dict:
        found = stream()
        store, key = found if found else (None, None)
        n, first, last = store.bar_bounds(*key) if found else (0, None, None)
        return {
            "source": get_source(), "symbol": get_engine().symbol, "clock": get_clock(),
            "can_import": found is not None,
            "stored": {"bars": n, "first_unix": first, "last_unix": last},
            "days": store.bar_days(*key) if found else [],
        }

    @r.get("")
    def get_history() -> dict:
        """Stored M1 coverage of the running source and symbol, per day."""
        return status()

    @r.post("/import")
    async def stage_import(request: Request) -> dict:
        """Upload an M1 file (raw body); returns a preview. Nothing is stored until the confirm call."""
        need_stream()
        data = await request.body()
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "The file is larger than 500 MB")
        try:
            return staged.stage(data)
        except BarsFileError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.post("/import/{token}")
    def confirm_import(token: str, body: dict = Body(...)) -> dict:
        """Store a staged file's bars, moved from the file's clock (``clock``) to the feed's."""
        store, key = need_stream()
        try:
            parsed = staged.take(token)
            result = import_bars(store, key, parsed, str(body.get("clock") or get_clock()), get_clock())
        except BarsFileError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"imported": result, **status()}

    return r
