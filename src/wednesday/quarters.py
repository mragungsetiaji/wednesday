"""Quarterly theory: the week, the day and each session cut into quarters.

Time runs in New York hours, with the trading day starting at 18:00 NY:

- week:    Mon (Q1), Tue (Q2), Wed (Q3), Thu (Q4), Fri
- session: the day in four 6-hour quarters: Tokyo 18:00, London 00:00,
           NY AM 06:00, NY PM 12:00 (New York time)
- 90m:     each session in four 90-minute quarters, Q1-Q4

Every block carries the open, high, low and close of the M1 bars inside it, so
the dashboard can colour it by direction (close above open is green). The
stats count how often each weekday, session and 90-minute quarter closed green.

Bar times are in the feed's clock. ``clock`` says what that clock is:
``UTC``, ``NY+7`` (New York time plus 7 hours, the server time of most MT5
gold brokers: midnight there is the 17:00 NY close), ``UTC+3`` style fixed
offsets, or an IANA zone such as ``Europe/London``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

NEW_YORK = "America/New_York"
DAY_SHIFT = pd.Timedelta(hours=6)  # 18:00 NY + 6h = 00:00 of the trading day

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
SESSIONS = ("Tokyo", "London", "NY AM", "NY PM")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
ROWS = ("week", "session", "q90")

_OFFSET = re.compile(r"^(UTC|NY)([+-]\d{1,2}(?:\.5)?)?$")


def clock_error(clock: str) -> str | None:
    """Why ``clock`` isn't a usable feed clock, or None."""
    if _OFFSET.match(clock):
        return None
    try:
        ZoneInfo(clock)
    except (ZoneInfoNotFoundError, ValueError):
        return f"Unknown clock {clock!r}: use UTC, NY+7, UTC+3 or a time zone like Europe/London"
    return None


def to_new_york(index: pd.DatetimeIndex, clock: str) -> pd.DatetimeIndex:
    """Naive feed times -> naive New York wall-clock times."""
    index = pd.DatetimeIndex(index)
    m = _OFFSET.match(clock)
    if m:
        base, off = m.group(1), float(m.group(2) or 0)
        shifted = index - pd.Timedelta(hours=off)
        if base == "NY":
            return shifted  # already New York wall time once the offset is removed
        utc = shifted.tz_localize("UTC")
    else:
        utc = index.tz_localize(clock, ambiguous="NaT", nonexistent="shift_forward").tz_convert("UTC")
    return utc.tz_convert(NEW_YORK).tz_localize(None)


@dataclass
class Block:
    row: str
    label: str
    day: pd.Timestamp  # trading day
    start: pd.Timestamp  # first M1 bar (feed clock)
    end: pd.Timestamp  # last M1 bar + 1 minute (feed clock)
    open: float
    high: float
    low: float
    close: float
    live: bool

    def to_dict(self) -> dict:
        return {
            "row": self.row,
            "label": self.label,
            "day": self.day.date().isoformat(),
            "start_unix": int(self.start.timestamp()),
            "end_unix": int(self.end.timestamp()),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "change": round(self.close - self.open, 5),
            "live": self.live,
        }


def _blocks(m1: pd.DataFrame, row: str, key: np.ndarray, labels: np.ndarray, period_end: np.ndarray,
            day: np.ndarray, now_shifted: pd.Timestamp) -> list[Block]:
    # A new block starts wherever the key changes between consecutive bars.
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(key)]
    o, h, l, c = (m1[col].to_numpy() for col in ("open", "high", "low", "close"))
    out = []
    for s, e in zip(starts, ends):
        out.append(Block(
            row=row, label=str(labels[s]), day=pd.Timestamp(day[s]),
            start=m1.index[s], end=m1.index[e - 1] + pd.Timedelta(minutes=1),
            open=float(o[s]), high=float(h[s:e].max()), low=float(l[s:e].min()), close=float(c[e - 1]),
            live=bool(e == len(key) and now_shifted.value < period_end[s]),
        ))
    return out


def quarter_blocks(m1: pd.DataFrame, clock: str) -> dict[str, list[Block]]:
    """Blocks per row (``week``, ``session``, ``q90``), oldest first."""
    if m1 is None or m1.empty:
        return {r: [] for r in ROWS}
    # Shifted New York time: the trading day runs 00:00-24:00 in it.
    shifted = to_new_york(m1.index, clock) + DAY_SHIFT
    keep = ~shifted.isna() & (shifted.dayofweek < 5)  # Sunday evening NY belongs to Monday; drop weekend bars
    m1, shifted = m1[keep], shifted[keep]
    if m1.empty:
        return {r: [] for r in ROWS}
    day = shifted.normalize().as_unit("ns")  # explicit unit: the period arithmetic below is in nanoseconds
    minute = (shifted - day) // pd.Timedelta(minutes=1)
    session = (minute // 360).to_numpy()
    q90 = ((minute % 360) // 90).to_numpy()
    day_ns = day.asi8
    day_arr = day.to_numpy()
    now = (shifted[-1] + pd.Timedelta(minutes=1)).as_unit("ns")
    one_day = pd.Timedelta(days=1).value

    week = _blocks(m1, "week", day_ns, np.array(WEEKDAYS)[day.dayofweek.to_numpy()],
                   day_ns + one_day, day_arr, now)
    sess_key = day_ns + session
    sess = _blocks(m1, "session", sess_key, np.array(SESSIONS)[session],
                   day_ns + (session + 1) * pd.Timedelta(hours=6).value, day_arr, now)
    q_key = sess_key * 4 + q90
    q = _blocks(m1, "q90", q_key, np.array(QUARTERS)[q90],
                day_ns + session * pd.Timedelta(hours=6).value + (q90 + 1) * pd.Timedelta(minutes=90).value,
                day_arr, now)
    return {"week": week, "session": sess, "q90": q}


def quarter_stats(blocks: dict[str, list[Block]]) -> dict[str, list[dict]]:
    """How often each weekday / session / quarter closed green, over the finished blocks."""
    order = {"week": WEEKDAYS, "session": SESSIONS, "q90": QUARTERS}
    out = {}
    for row, labels in order.items():
        done = [b for b in blocks.get(row, []) if not b.live]
        stats = []
        for label in labels:
            moves = [b.close - b.open for b in done if b.label == label]
            stats.append({
                "label": label,
                "count": len(moves),
                "green": sum(1 for m in moves if m > 0),
                "avg_change": round(float(np.mean(moves)), 5) if moves else None,
            })
        out[row] = stats
    return out


def quarters_payload(m1: pd.DataFrame, clock: str) -> dict:
    blocks = quarter_blocks(m1, clock)
    return {
        "clock": clock,
        "rows": {row: [b.to_dict() for b in bs] for row, bs in blocks.items()},
        "stats": quarter_stats(blocks),
    }
