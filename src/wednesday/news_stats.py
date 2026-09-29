"""How gold moved after past releases, per event type, from the stored calendar and M1 bars.

The weekly calendar feed is kept in the database as it's fetched (``calendar_events``),
and past calendars can be imported from CSV. For each past release with M1 bars
around it, :func:`reaction` measures, on the feed clock (the release's UTC time
converted with :func:`quarters.utc_to_feed`, as for the news lines):

* the move 5, 15 and 60 minutes after the release, from the last close before it,
  in price and in ATRs (ATR(14) of the 15M bars before the release);
* the high-low range of the first 15 minutes;
* whether the first move reversed: the 60-minute move is on the other side of the
  pre-release price from the 5-minute one;
* whether the actual figure came in above or below the forecast, when both are known.

Releases without bars from just before to an hour after are left out, and the
counts say how many were used. These are descriptions of past moves, not a forecast.
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from datetime import datetime, timezone
from statistics import median
from typing import Callable

import numpy as np
import pandas as pd

from .quarters import utc_to_feed
from .storage import Store

WINDOWS = (5, 15, 60)  # minutes after the release
LIMIT = 24  # most recent covered releases per event type
MIN_AFTER = 50  # of the 60 M1 bars after the release
ATR_BARS = 14  # 15M bars before the release for the ATR
CACHE_TTL = 600  # seconds
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 50_000
CSV_ZONES = ("UTC", "America/New_York")

BarsFn = Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame]  # feed-clock start, end -> M1 bars


class CalendarImportError(ValueError):
    """A CSV that can't be read, with a message saying what to fix."""


def event_key(currency: str, title: str) -> str:
    """One event type: "USD CPI m/m" (spaces and case normalised)."""
    return f"{currency.strip().upper()} {' '.join(title.split())}".lower()


def store_rows(events: list[dict], source: str = "feed") -> list[dict]:
    """Calendar events (ISO UTC ``time``) -> ``calendar_events`` rows."""
    out = []
    for e in events:
        try:
            when = int(datetime.fromisoformat(str(e["time"])).timestamp())
        except (KeyError, ValueError):
            continue
        out.append({"time": when, "currency": str(e.get("currency") or "").upper()[:8],
                    "title": " ".join(str(e.get("title") or "").split())[:200], "impact": e.get("impact") or None,
                    "forecast": _figure(e.get("forecast")), "previous": _figure(e.get("previous")),
                    "actual": _figure(e.get("actual")), "source": source})
    return [r for r in out if r["currency"] and r["title"]]


def _figure(v) -> str | None:
    v = str(v).strip() if v is not None else ""
    return v[:40] or None


_NUM = re.compile(r"^[<>~]?\s*([+\-−]?\d+(?:[.,]\d+)?)\s*([KMBT%]?)", re.I)
_SCALE = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def number(text: str | None) -> float | None:
    """ "0.3%" -> 0.3, "-12K" -> -12000, "1.2M" -> 1200000; None when it isn't a figure."""
    m = _NUM.match(str(text or "").strip())
    if not m:
        return None
    v = float(m.group(1).replace("−", "-").replace(",", "."))
    return v * _SCALE.get(m.group(2).upper(), 1)


def surprise(actual: str | None, forecast: str | None) -> str | None:
    """ "above", "below" or "inline" the forecast; None without both figures."""
    a, f = number(actual), number(forecast)
    if a is None or f is None:
        return None
    return "above" if a > f else "below" if a < f else "inline"


def _atr(before: pd.DataFrame) -> float | None:
    m15 = before.resample("15min", label="left", closed="left").agg(
        {"high": "max", "low": "min", "close": "last"}).dropna()
    if len(m15) < ATR_BARS // 2 + 1:
        return None
    prev = m15["close"].shift(1)
    tr = pd.concat([m15["high"] - m15["low"], (m15["high"] - prev).abs(), (m15["low"] - prev).abs()], axis=1).max(axis=1)
    atr = float(tr.iloc[1:].tail(ATR_BARS).mean())
    return atr if atr > 0 else None


def reaction(m1: pd.DataFrame, t0: pd.Timestamp) -> dict | None:
    """Gold's move after one release at ``t0`` (naive, feed clock), or None without the bars for it."""
    if m1 is None or m1.empty:
        return None
    before = m1[m1.index < t0]
    after = m1[(m1.index >= t0) & (m1.index < t0 + pd.Timedelta(minutes=60))]
    if before.empty or before.index[-1] < t0 - pd.Timedelta(minutes=5):
        return None
    if (len(after) < MIN_AFTER or after.index[0] > t0 + pd.Timedelta(minutes=2)
            or after.index[-1] < t0 + pd.Timedelta(minutes=55)):
        return None
    ref = float(before["close"].iloc[-1])
    moves = {}
    for n in WINDOWS:
        part = after[after.index < t0 + pd.Timedelta(minutes=n)]
        moves[n] = round(float(part["close"].iloc[-1]) - ref, 2)
    first = after[after.index < t0 + pd.Timedelta(minutes=15)]
    atr = _atr(before[before.index >= t0 - pd.Timedelta(minutes=15 * (ATR_BARS + 1))])
    return {
        "moves": moves,
        "range15": round(float(first["high"].max() - first["low"].min()), 2),
        "atr": round(atr, 2) if atr else None,
        "reversed": bool(moves[5] != 0 and np.sign(moves[60]) == -np.sign(moves[5])),
    }


def _med(values: list[float]) -> float | None:
    return round(float(median(values)), 2) if values else None


def summarise(title: str, currency: str, rows: list[dict], stored: int) -> dict:
    """One event type from its covered releases (newest first)."""
    n = len(rows)
    atr_rows = [r for r in rows if r["atr"]]
    by = {side: [r for r in rows if r["surprise"] == side] for side in ("above", "below")}
    out = {
        "title": title, "currency": currency, "count": n, "stored": stored,
        "last": rows[0]["time"] if rows else None,
        "move": {str(w): _med([abs(r["moves"][w]) for r in rows]) for w in WINDOWS},
        "move_atr": {str(w): _med([abs(r["moves"][w]) / r["atr"] for r in atr_rows]) for w in WINDOWS},
        "range15": _med([r["range15"] for r in rows]),
        "range15_atr": _med([r["range15"] / r["atr"] for r in atr_rows]),
        "reversed": sum(r["reversed"] for r in rows),
        "up15": sum(r["moves"][15] > 0 for r in rows),
        "surprise": {side: {"count": len(v), "move15": _med([r["moves"][15] for r in v])} for side, v in by.items()},
    }
    out["summary"] = summary_line(out)
    return out


def summary_line(s: dict) -> str:
    """ "CPI m/m: median 15m range 9.40 (last 12)" """
    if not s["count"]:
        return f"{s['title']}: no M1 bars around past releases yet"
    return f"{s['title']}: median 15m range {s['range15']:.2f} (last {s['count']})"


def brief_line(s: dict) -> str:
    """The same facts, longer, for the LLM brief's context."""
    if not s["count"]:
        return "no past release with M1 bars stored"
    m, a = s["move"], s["move_atr"]
    atr = f" ({a['15']:.1f} ATR)" if a["15"] is not None else ""
    parts = [f"last {s['count']} release{'s' if s['count'] != 1 else ''}: median 15m range {s['range15']:.2f}"
             + (f" ({s['range15_atr']:.1f} ATR)" if s["range15_atr"] is not None else ""),
             f"median size of the move after 5m {m['5']:.2f}, 15m {m['15']:.2f}{atr}, 60m {m['60']:.2f}",
             f"15m move up {s['up15']} of {s['count']}",
             f"first move reversed within the hour {s['reversed']} of {s['count']}"]
    for side in ("above", "below"):
        v = s["surprise"][side]
        if v["count"]:
            parts.append(f"actual {side} forecast {v['count']}x, median 15m move {v['move15']:+.2f}")
    return "; ".join(parts)


class NewsReactions:
    """Per event type stats over the stored calendar, cached for a few minutes."""

    def __init__(self, store: Store):
        self.store = store
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def stats(self, bars: BarsFn, clock: str, currencies: list[str], impacts: list[str],
              key: tuple = (), now: float | None = None) -> dict[str, dict]:
        """event key -> summary, over releases at least an hour old. ``key`` names the bars (source, symbol)."""
        now = now or time.time()
        ck = (clock, tuple(currencies), tuple(impacts), key, self.store.calendar_bounds())
        with self._lock:
            hit = self._cache.get(ck)
            if hit and now - hit[0] < CACHE_TTL:
                return hit[1]
        groups: dict[str, list[dict]] = {}
        for e in self.store.calendar_history(currencies, impacts, before=int(now) - 61 * 60):
            groups.setdefault(event_key(e["currency"], e["title"]), []).append(e)
        out = {}
        for k, events in groups.items():
            rows = []
            for e in events:  # newest first
                if len(rows) >= LIMIT:
                    break
                t0 = utc_to_feed(pd.Timestamp(e["time"], unit="s", tz="UTC"), clock)
                got = reaction(bars(t0 - pd.Timedelta(minutes=15 * (ATR_BARS + 2)), t0 + pd.Timedelta(minutes=61)), t0)
                if got:
                    rows.append({**got, "time": e["time"], "surprise": surprise(e["actual"], e["forecast"])})
            out[k] = summarise(events[0]["title"], events[0]["currency"], rows, len(events))
        with self._lock:
            self._cache[ck] = (now, out)
        return out


# ---- CSV import ----------------------------------------------------------------------------

_COLS = {
    "datetime": ("datetime", "date_time", "timestamp"),
    "date": ("date", "day"),
    "time": ("time",),
    "currency": ("currency", "country", "ccy"),
    "title": ("title", "event", "name"),
    "impact": ("impact", "importance"),
    "actual": ("actual",),
    "forecast": ("forecast", "consensus"),
    "previous": ("previous", "prior"),
}


def parse_csv(data: bytes, zone: str = "UTC") -> list[dict]:
    """A past calendar as CSV -> ``calendar_events`` rows.

    Columns (header names, any case): ``currency`` (or ``country``), ``title`` (or
    ``event``), and either ``datetime`` or ``date`` + ``time``; optional ``impact``,
    ``actual``, ``forecast``, ``previous``. Times without an offset are read in
    ``zone`` (UTC or America/New_York). Rows without a clock time (All Day,
    Tentative) are skipped.
    """
    if zone not in CSV_ZONES:
        raise CalendarImportError(f"The time zone is one of {', '.join(CSV_ZONES)}")
    if len(data) > MAX_CSV_BYTES:
        raise CalendarImportError("The file is larger than 5 MB; split it")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
    col = {k: next((fields[a] for a in names if a in fields), None) for k, names in _COLS.items()}
    if not col["currency"] or not col["title"] or not (col["datetime"] or col["date"]):
        raise CalendarImportError("The CSV needs a header with currency (or country), title (or event), "
                                  "and datetime, or date and time")
    out = []
    for i, row in enumerate(reader):
        if i >= MAX_CSV_ROWS:
            raise CalendarImportError(f"More than {MAX_CSV_ROWS} rows; split the file")
        get = lambda k: (row.get(col[k]) or "").strip() if col[k] else ""  # noqa: E731
        stamp = get("datetime") or f"{get('date')} {get('time')}".strip()
        if not re.search(r"\d{1,2}:\d{2}|\d{1,2}\s*(am|pm)", stamp, re.I):
            continue  # all day, tentative, or no time
        try:
            when = pd.Timestamp(re.sub(r"(\d)(am|pm)\b", r"\1 \2", stamp, flags=re.I))
        except (ValueError, TypeError):
            raise CalendarImportError(f"Row {i + 2}: can't read the time {stamp!r}") from None
        when = when.tz_localize(zone) if when.tzinfo is None else when
        impact = get("impact").capitalize() or None
        out.extend(store_rows([{"time": when.tz_convert("UTC").isoformat(), "currency": get("currency"),
                                "title": get("title"), "impact": impact, "actual": get("actual"),
                                "forecast": get("forecast"), "previous": get("previous")}], source="csv"))
    if not out:
        raise CalendarImportError("No row with a release time was found")
    return out


def history_summary(store: Store) -> dict:
    n, first, last = store.calendar_bounds()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat() if t is not None else None  # noqa: E731
    return {"stored": n, "first": iso(first), "last": iso(last)}
