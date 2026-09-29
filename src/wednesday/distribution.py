"""How unusual is the current move: past periods' moves as a histogram, with the current
one placed in it by percentile (first) and z-score.

Periods come from the quarter blocks (:mod:`quarters`: New York time, the trading day
starting 18:00 the evening before, so boundaries follow the 17:00 New York close through
DST): ``day``, ``week`` (a trading week's days), ``session`` (Tokyo, London, NY AM, NY PM)
and ``q90`` (a session's 90-minute quarter).

Measures:

* ``change``: the move in %, close to the previous close for a day or a week, open to
  close for a session or a 90-minute block;
* ``range_pct``: high minus low, in % of the open;
* ``range_atr``: high minus low in daily ATR(14), the true range of the 14 days before.

Only closed periods are samples. The current (last) period is the marker, flagged
``forming`` while it runs. Gold's moves have fat tails, so the empirical percentile is the
honest number; the z-score read through a normal curve understates how often big moves
happen. Under ``MIN_SAMPLES`` neither is given.

This is context for the trader, not a signal: nothing here sets the bias or trades.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .quarters import SESSIONS, WEEKDAYS, Block

PERIODS = ("day", "week", "session", "q90")
MEASURES = ("change", "range_pct", "range_atr")
LOOKBACK_DAYS = {"1y": 365, "5y": 1826, "all": None}
MIN_SAMPLES = 30
ATR_DAYS = 14
LIKE_THIS = 20  # past periods listed as "like this"
COLUMNS = ["day", "label", "start", "open", "high", "low", "close", "live"]


def _frame(blocks: list[Block]) -> pd.DataFrame:
    if not blocks:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame([{"day": b.day, "label": b.label, "start": b.start, "open": b.open, "high": b.high,
                          "low": b.low, "close": b.close, "live": b.live} for b in blocks])


def daily_from_d1(d1: pd.DataFrame | None) -> pd.DataFrame:
    """Daily bars from the feed (indexed by trading day) as a day table."""
    if d1 is None or d1.empty:
        return pd.DataFrame(columns=COLUMNS)
    days = pd.DatetimeIndex(d1.index).normalize()
    keep = days.dayofweek < 5
    d1, days = d1[keep], days[keep]
    return pd.DataFrame({"day": days, "label": np.array(WEEKDAYS)[days.dayofweek], "start": d1.index,
                         "open": d1["open"].to_numpy(float), "high": d1["high"].to_numpy(float),
                         "low": d1["low"].to_numpy(float), "close": d1["close"].to_numpy(float), "live": False})


def tables(blocks: dict[str, list[Block]], d1: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """One table per period, oldest first. Daily bars from the feed extend the days (and weeks)
    back before the M1 history."""
    day = _frame(blocks.get("week", []))  # the quarter pane's "week" row is one block per day
    older = daily_from_d1(d1)
    if len(older):
        if len(day):
            older = older[older["day"] < day["day"].min()]
        day = pd.concat([older, day], ignore_index=True) if len(day) else older
    day = day.reset_index(drop=True)

    if len(day):
        monday = day["day"] - pd.to_timedelta(day["day"].dt.dayofweek, unit="D")
        g = day.groupby(monday, sort=True)
        week = pd.DataFrame({"day": g["day"].first(), "start": g["start"].first(), "open": g["open"].first(),
                             "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
                             "last_day": g["day"].last(), "last_live": g["live"].last()}).reset_index(drop=True)
        week["label"] = "Week of " + week["day"].dt.strftime("%d %b")
        # The last week still runs until its Friday closes.
        week["live"] = False
        if len(week):
            i = len(week) - 1
            week.loc[i, "live"] = bool(week.loc[i, "last_live"] or week.loc[i, "last_day"].dayofweek < 4)
        week = week[COLUMNS]
    else:
        week = pd.DataFrame(columns=COLUMNS)

    session = _frame(blocks.get("session", []))
    q90 = _frame(blocks.get("q90", []))
    if len(q90) and len(session):
        # Each 90-minute block's session: the session that started last at or before it.
        s = session[["start", "label"]].rename(columns={"label": "session"}).sort_values("start")
        q90 = pd.merge_asof(q90.sort_values("start"), s, on="start", direction="backward")
        q90["label"] = q90["session"].fillna("") + " " + q90["label"]
        q90 = q90[COLUMNS]
    return {"day": day, "week": week, "session": session, "q90": q90}


def _daily_atr(day: pd.DataFrame) -> pd.Series:
    """ATR(14) of the days before each day (never the day itself), by trading day."""
    if not len(day):
        return pd.Series(dtype=float)
    prev = day["close"].shift(1)
    tr = np.maximum(day["high"], prev.fillna(day["high"])) - np.minimum(day["low"], prev.fillna(day["low"]))
    atr = tr.rolling(ATR_DAYS, min_periods=ATR_DAYS).mean().shift(1)
    return pd.Series(atr.to_numpy(), index=pd.DatetimeIndex(day["day"]))


def values(t: dict[str, pd.DataFrame], period: str, measure: str) -> pd.DataFrame:
    """The period table with each row's ``value`` for the measure (NaN where it can't be had)."""
    df = t[period].copy()
    if not len(df):
        df["value"] = pd.Series(dtype=float)
        return df
    if measure == "change":
        base = df["close"].shift(1) if period in ("day", "week") else df["open"]
        df["value"] = (df["close"] / base - 1) * 100
    elif measure == "range_pct":
        df["value"] = (df["high"] - df["low"]) / df["open"] * 100
    elif measure == "range_atr":
        atr = _daily_atr(t["day"])
        day_atr = atr.reindex(pd.DatetimeIndex(df["day"])).to_numpy()
        df["value"] = (df["high"] - df["low"]).to_numpy(float) / day_atr
    else:
        raise ValueError(f"measure is one of {', '.join(MEASURES)}")
    return df


def _session_of(label: str) -> str:
    return next((s for s in SESSIONS if label.startswith(s)), label)


def distribution(t: dict[str, pd.DataFrame], period: str = "day", measure: str = "change", lookback: str = "1y",
                 same_weekday: bool = False, same_session: bool = False, bins: int = 40) -> dict:
    if period not in PERIODS:
        raise ValueError(f"period is one of {', '.join(PERIODS)}")
    if lookback not in LOOKBACK_DAYS:
        raise ValueError(f"lookback is one of {', '.join(LOOKBACK_DAYS)}")
    df = values(t, period, measure)
    unit = "×ATR" if measure == "range_atr" else "%"
    out: dict = {"period": period, "measure": measure, "unit": unit, "lookback": lookback,
                 "min_samples": MIN_SAMPLES, "samples": 0, "current": None, "stats": None, "percentile": None,
                 "z": None, "histogram": {"edges": [], "counts": []}, "like_this": [],
                 "filters": {"weekday": None, "session": None}, "coverage": None}
    if not len(df):
        return out
    df = df.reset_index(drop=True)
    df["next"] = df["value"].shift(-1)
    cur = df.iloc[-1]
    closed = df.iloc[:-1]  # the last period is the marker, forming or (market closed) the latest closed one
    days = LOOKBACK_DAYS[lookback]
    if days is not None:
        closed = closed[closed["day"] >= cur["day"] - pd.Timedelta(days=days)]
    if same_weekday and period != "week":
        wd = cur["day"].dayofweek
        closed = closed[closed["day"].dt.dayofweek == wd]
        out["filters"]["weekday"] = WEEKDAYS[wd] if wd < 5 else None
    if same_session and period in ("session", "q90"):
        closed = closed[closed["label"] == cur["label"]] if period == "q90" else \
            closed[closed["label"].map(_session_of) == _session_of(cur["label"])]
        out["filters"]["session"] = cur["label"]
    closed = closed[np.isfinite(closed["value"].astype(float))]
    v = closed["value"].to_numpy(float)
    x = float(cur["value"]) if np.isfinite(cur["value"]) else None
    out["samples"] = int(len(v))
    out["coverage"] = {"from": closed["day"].min().date().isoformat() if len(v) else None,
                       "to": closed["day"].max().date().isoformat() if len(v) else None}
    out["current"] = {"day": cur["day"].date().isoformat(), "label": str(cur["label"]), "value": x,
                      "forming": bool(cur["live"]), "start_unix": int(pd.Timestamp(cur["start"]).timestamp())}
    if not len(v):
        return out

    mean, median, std = float(v.mean()), float(np.median(v)), float(v.std(ddof=1)) if len(v) > 1 else 0.0
    out["stats"] = {"mean": mean, "median": median, "std": std, "min": float(v.min()), "max": float(v.max())}
    lo = min(v.min(), x) if x is not None else v.min()
    hi = max(v.max(), x) if x is not None else v.max()
    n_bins = int(min(max(bins, 10), 80))
    edges = np.linspace(lo, hi, n_bins + 1) if hi > lo else np.array([lo - 0.5, hi + 0.5])
    counts, _ = np.histogram(v, bins=edges)
    out["histogram"] = {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts]}

    if x is not None and len(v) >= MIN_SAMPLES:
        low_side = x <= median
        tail = float(np.mean(v <= x) if low_side else np.mean(v >= x))
        out["percentile"] = {"below": float(np.mean(v < x)), "above": float(np.mean(v > x)), "tail": tail,
                             "side": "low" if low_side else "high"}
        out["z"] = (x - mean) / std if std > 0 else None
        like = closed[(closed["value"] <= x) if low_side else (closed["value"] >= x)].sort_values("day", ascending=False)
        out["like_this"] = [{"day": r["day"].date().isoformat(), "label": str(r["label"]), "value": float(r["value"]),
                             "next": float(r["next"]) if np.isfinite(r["next"]) else None}
                            for _, r in like.head(LIKE_THIS).iterrows()]
    return out
