"""More M1 history for the Lab (and the chart): import CSV files, backfill from MT5.

Both write into the stored bars of the running source and symbol, as an upsert
on time, so importing a file twice or overlapping a backfill adds nothing twice.

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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from typing import TYPE_CHECKING

import pandas as pd

from ..quarters import clock_error, convert_clock
from ..timeframes import OHLCV_COLUMNS, normalize_ohlcv

if TYPE_CHECKING:
    from ..engine import Engine
    from ..storage import Store

log = logging.getLogger(__name__)

MAX_IMPORT_BYTES = 500 * 1024 * 1024
BACKFILL_CHUNK = pd.Timedelta(days=5)  # ~7k M1 bars per terminal call; scans run in between
HISTORY_BARS_DEFAULT = 400_000  # about a year of M1 bars
HISTORY_BARS_MAX = 2_000_000  # about five years; ~100 MB in memory
LAB_DATA_KEY = "lab_data"


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


class Backfill:
    """Pulls older M1 bars from the MT5 terminal into the database, a chunk at a time, on a thread."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.state: dict = {"running": False, "start": None, "reached": None, "added": 0, "error": None,
                            "note": None, "finished_at": None}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, engine: Engine, store: Store, start: pd.Timestamp, on_done: Callable[[], None] | None = None) -> None:
        if self.running:
            raise RuntimeError("A backfill is already running")
        key = engine.buffer.key
        if not engine.feed.native_history or key is None:
            raise RuntimeError("Backfill reads from the MT5 terminal: make MT5 the data source first")
        self._cancel.clear()
        self.state = {"running": True, "start": start.isoformat(), "reached": None, "added": 0, "error": None,
                      "note": None, "finished_at": None}
        self._thread = threading.Thread(target=self._run, args=(engine, store, key, start, on_done),
                                        name="lab-backfill", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self, engine: Engine, store: Store, key: tuple[str, str], start: pd.Timestamp, on_done) -> None:
        try:
            _, first, _ = store.bar_bounds(*key)
            _, _, live = engine.snapshot()
            if first is not None:
                end = pd.Timestamp(first, unit="s")
            elif live is not None and len(live):
                end = live.index[0]
            else:
                end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("min")
            empty = 0
            while end > start and not self._cancel.is_set():
                begin = max(start, end - BACKFILL_CHUNK)
                bars = engine.call(lambda feed, b=begin, e=end: feed.fetch_m1_range(b, e), timeout=120)
                bars = bars[(bars.index >= begin) & (bars.index < end)]
                if bars.empty:
                    empty += 1
                    if empty >= 3:  # 15 days with nothing: the terminal has no older M1 history
                        self.state["note"] = ("The terminal has no M1 bars before "
                                              f"{end:%Y-%m-%d}. In MT5, raise Tools > Options > Charts > Max bars "
                                              "in chart and scroll the M1 chart back so it downloads more, then run "
                                              "the backfill again.")
                        break
                else:
                    empty = 0
                    before, _, _ = store.bar_bounds(*key)
                    store.save_bars(*key, bars)
                    self.state["added"] += store.bar_bounds(*key)[0] - before
                end = begin
                self.state["reached"] = end.isoformat()
                time.sleep(0.05)
            if self._cancel.is_set():
                self.state["note"] = "Stopped."
        except Exception as exc:  # noqa: BLE001 - shown in the Lab
            log.exception("backfill failed")
            self.state["error"] = str(exc)
        finally:
            self.state["running"] = False
            self.state["finished_at"] = datetime.now(timezone.utc).isoformat()
            if on_done:
                on_done()
