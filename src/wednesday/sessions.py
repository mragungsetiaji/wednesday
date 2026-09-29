"""Session shading and reference levels for the price chart.

Everything is set in New York time and handed to the chart in the feed's clock, so it
stays right through DST whatever the broker's clock (:func:`quarters.to_new_york`,
:func:`quarters.utc_to_feed`). The trading day runs 18:00 New York the evening before
to 17:00, like the quarterly pane.

* Killzones (shading): Asia 20:00-00:00, London 02:00-05:00, NY AM 07:00-10:00,
  NY PM 13:30-16:00 New York time.
* Asia high / low: the Asia killzone's range, drawn on from 00:00 until price takes it
  out, or to the day's close.
* Previous day, week and month high / low, each drawn across the next period and swept
  (faded from then on) the first time a closed bar trades through it. A period only
  gives levels once its whole range is in the history.
* Opens: the day's (18:00), the week's (Sunday 18:00), midnight New York (00:00) and
  the current 90-minute quarter's.

Only closed M1 bars go in: the feed never hands over the forming one, the Asia range
waits for its window to close, and a sweep is a closed bar's high or low.
"""

from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from .quarters import DAY_SHIFT, NEW_YORK, to_new_york, utc_index_to_feed

# name, start (NY wall time; the day before when it is at or after 18:00), end
KILLZONES = (
    ("Asia", dtime(20, 0), dtime(0, 0)),
    ("London", dtime(2, 0), dtime(5, 0)),
    ("NY AM", dtime(7, 0), dtime(10, 0)),
    ("NY PM", dtime(13, 30), dtime(16, 0)),
)
DAY_CLOSE = dtime(17, 0)
MAX_DAYS = 30  # trading days of shading and daily levels sent to the chart

# Line kinds, grouped by the chart's toggles.
GROUPS = {
    "killzones": (),
    "asia": ("asia_high", "asia_low"),
    "day": ("pdh", "pdl"),
    "week": ("pwh", "pwl"),
    "month": ("pmh", "pml"),
    "opens": ("day_open", "week_open", "midnight_open"),
    "quarter": ("quarter_open",),
}
LABELS = {
    "asia_high": "Asia H", "asia_low": "Asia L", "pdh": "PDH", "pdl": "PDL", "pwh": "PWH", "pwl": "PWL",
    "pmh": "PMH", "pml": "PML", "day_open": "Day open", "week_open": "Week open", "midnight_open": "00:00 open",
    "quarter_open": "Q open",
}


def _ny_wall(day: pd.Timestamp, at: dtime) -> pd.Timestamp:
    """The New York wall time ``at`` belonging to trading day ``day`` (18:00 and later: the evening before)."""
    date = day - pd.Timedelta(days=1) if at >= dtime(18, 0) else day
    return pd.Timestamp.combine(date.date(), at)


def _to_feed(walls: list[pd.Timestamp], clock: str) -> np.ndarray:
    """New York wall times -> feed-clock unix seconds."""
    if not walls:
        return np.array([], dtype=np.int64)
    aware = pd.DatetimeIndex(walls).tz_localize(NEW_YORK, ambiguous=True, nonexistent="shift_forward")
    return (utc_index_to_feed(aware, clock).as_unit("s").asi8).astype(np.int64)


def _periods(day: pd.Series, freq: str) -> pd.Series:
    if freq == "day":
        return day
    if freq == "week":
        return day - pd.to_timedelta(day.dt.dayofweek, unit="D")
    return day.dt.to_period("M").dt.start_time


def reference_levels(m1: pd.DataFrame, clock: str, max_days: int = MAX_DAYS) -> dict:
    """Killzones and levels over the last ``max_days`` trading days of ``m1`` (closed M1 bars,
    feed clock). Times are feed-clock unix seconds, as the chart's."""
    out: dict = {"clock": clock, "killzones": [], "lines": []}
    if m1 is None or m1.empty:
        return out
    shifted = to_new_york(m1.index, clock) + DAY_SHIFT
    keep = ~shifted.isna() & (shifted.dayofweek < 5)
    m1, shifted = m1[keep], shifted[keep]
    if m1.empty:
        return out
    t = (pd.DatetimeIndex(m1.index).as_unit("s").asi8).astype(np.int64)
    hi, lo, op = (m1[c].to_numpy(float) for c in ("high", "low", "open"))
    bars = pd.DataFrame({"t": t, "high": hi, "low": lo, "open": op, "day": shifted.normalize(), "shifted": shifted})
    last_t = int(t[-1])

    def first_through(price: float, above: bool, start: int, end: int) -> int | None:
        """The first closed bar in [start, end) that trades through ``price``."""
        a, b = np.searchsorted(t, start), np.searchsorted(t, end)
        if a >= b:
            return None
        hit = hi[a:b] > price if above else lo[a:b] < price
        i = int(np.argmax(hit))
        return int(t[a + i]) if hit[i] else None

    def line(kind: str, price: float, start: int, end: int, sweep_from: int | None = None, current: bool = False,
             sweepable: bool = True) -> None:
        swept = None
        if sweepable:
            above = kind in ("asia_high", "pdh", "pwh", "pmh")
            swept = first_through(price, above, sweep_from if sweep_from is not None else start, end)
        out["lines"].append({"kind": kind, "label": LABELS[kind], "price": price, "start_unix": int(start),
                             "end_unix": int(end), "swept_unix": swept, "current": current})

    days = list(pd.DatetimeIndex(bars["day"].unique()))
    shown = days[-max_days:]
    last_day = days[-1]

    # Killzones: every shown day's windows, including those still to come today.
    walls, names = [], []
    for d in shown:
        for name, start, end in KILLZONES:
            walls += [_ny_wall(d, start), _ny_wall(d, end)]
            names.append(name)
    feed = _to_feed(walls, clock)
    out["killzones"] = [{"name": n, "day": d.date().isoformat(), "start_unix": int(feed[2 * i]), "end_unix": int(feed[2 * i + 1])}
                        for i, (n, d) in enumerate(zip(names, np.repeat(shown, len(KILLZONES))))]

    # Each shown day's bounds (feed clock): 18:00 the evening before to 17:00.
    bounds = _to_feed([w for d in shown for w in (_ny_wall(d, dtime(18, 0)), _ny_wall(d, DAY_CLOSE),
                                                   _ny_wall(d, dtime(0, 0)))], clock).reshape(-1, 3)
    asia = _to_feed([w for d in shown for w in (_ny_wall(d, KILLZONES[0][1]), _ny_wall(d, KILLZONES[0][2]))], clock).reshape(-1, 2)
    for i, d in enumerate(shown):
        day_start, day_end, midnight = (int(x) for x in bounds[i])
        a_start, a_end = (int(x) for x in asia[i])
        current = d == last_day
        in_day = bars["day"] == d
        # Opens: the first closed bar at or after the time; only when that bar is the one opening it.
        rows = bars[in_day]
        if len(rows) and rows["t"].iloc[0] < day_start + 3600:
            line("day_open", float(rows["open"].iloc[0]), day_start, day_end, current=current, sweepable=False)
        mid = rows[(rows["t"] >= midnight) & (rows["t"] < midnight + 3600)]
        if len(mid):
            line("midnight_open", float(mid["open"].iloc[0]), midnight, day_end, current=current, sweepable=False)
        # Asia: only once its window has closed, and only when the history covers all of it.
        if last_t + 60 >= a_end:
            win = rows[(rows["t"] >= a_start) & (rows["t"] < a_end)]
            if len(win) and win["t"].iloc[0] < a_start + 3600:
                line("asia_high", float(win["high"].max()), a_start, day_end, sweep_from=a_end, current=current)
                line("asia_low", float(win["low"].min()), a_start, day_end, sweep_from=a_end, current=current)

    # Previous day / week / month: each whole period's high and low, drawn across the next one.
    first_shifted = shifted[0]
    for freq, (hk, lk), open_kind in (("day", ("pdh", "pdl"), None), ("week", ("pwh", "pwl"), "week_open"),
                                       ("month", ("pmh", "pml"), None)):
        key = _periods(bars["day"], freq)
        g = bars.groupby(key.to_numpy(), sort=True)
        agg = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "open": g["open"].first(),
                            "first_t": g["t"].min(), "first_day": g["day"].min(), "last_day": g["day"].max()})
        periods = list(agg.index)
        for j, p in enumerate(periods):
            row = agg.loc[p]
            if row["last_day"] < shown[0] and j + 1 < len(periods) and agg.loc[periods[j + 1], "last_day"] < shown[0]:
                continue  # neither it nor the period it feeds is on screen
            nxt_first = agg.loc[periods[j + 1], "first_day"] if j + 1 < len(periods) else None
            # Bounds of period p itself, from its first to its last trading day.
            p_start = int(_to_feed([_ny_wall(row["first_day"], dtime(18, 0))], clock)[0])
            if open_kind and row["first_t"] < p_start + 3600 and row["last_day"] >= shown[0]:
                p_end = int(_to_feed([_ny_wall(_period_last_day(p, freq), DAY_CLOSE)], clock)[0])
                line(open_kind, float(row["open"]), p_start, p_end, current=j == len(periods) - 1, sweepable=False)
            if nxt_first is None:
                continue
            whole = j > 0 or first_shifted <= pd.Timestamp(row["first_day"]) + pd.Timedelta(hours=1) and \
                row["first_day"] == _period_first_weekday(p, freq)
            if not whole:
                continue
            q = periods[j + 1]
            s = int(_to_feed([_ny_wall(nxt_first, dtime(18, 0))], clock)[0])
            e = int(_to_feed([_ny_wall(_period_last_day(q, freq), DAY_CLOSE)], clock)[0])
            cur = j + 1 == len(periods) - 1
            line(hk, float(row["high"]), s, e, current=cur)
            line(lk, float(row["low"]), s, e, current=cur)

    # The current 90-minute quarter's open.
    minute = (shifted[-1] - shifted[-1].normalize()) // pd.Timedelta(minutes=1)
    q_start_shifted = shifted[-1].normalize() + pd.Timedelta(minutes=int(minute // 90 * 90))
    q_rows = bars[(bars["shifted"] >= q_start_shifted) & (bars["shifted"] < q_start_shifted + pd.Timedelta(minutes=90))]
    if len(q_rows) and q_rows["shifted"].iloc[0] < q_start_shifted + pd.Timedelta(minutes=5):
        q_wall = q_start_shifted - DAY_SHIFT
        s, e = (int(x) for x in _to_feed([q_wall, q_wall + pd.Timedelta(minutes=90)], clock))
        line("quarter_open", float(q_rows["open"].iloc[0]), s, e, current=True, sweepable=False)
    return out


def _period_last_day(p: pd.Timestamp, freq: str) -> pd.Timestamp:
    """The last weekday of the day / week / month starting at ``p``."""
    p = pd.Timestamp(p)
    if freq == "day":
        return p
    if freq == "week":
        return p + pd.Timedelta(days=4)
    end = p + pd.offsets.MonthEnd(0)
    while end.dayofweek >= 5:
        end -= pd.Timedelta(days=1)
    return end


def _period_first_weekday(p: pd.Timestamp, freq: str) -> pd.Timestamp:
    p = pd.Timestamp(p)
    while p.dayofweek >= 5:
        p += pd.Timedelta(days=1)
    return p
