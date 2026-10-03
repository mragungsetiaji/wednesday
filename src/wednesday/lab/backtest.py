"""Backtest the order block limit plan over the stored history.

Every order block the detector finds on a timeframe is traded with the same plan the
Lab trains on (:mod:`outcome`: entry on the body, stop capped at ``max_sl``, target in
R, a horizon), through the same :func:`outcome.simulate_trade`. The order goes in at
the close of the candle that broke structure (``confirmed_time`` + one candle), never
earlier, so nothing is traded on information it didn't have.

Filters:

* ``all``: every detector order block;
* ``model``: only blocks the active model gives a win chance of at least ``min_win``.
  Its features for a candle are ready ``confirm`` candles after it, so such a trade
  starts at the later of the break close and that time;
* ``labels``: only detector blocks you labelled as that order block (a "yes" label
  covering the block's candle).

Results are split by OB priority, swing tag, session and weekday (New York time, the
trading day starting 18:00 like :mod:`quarters`), with trades, fill rate, win rate,
average R and max drawdown in R for each.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..detectors.base import DetectorParams
from ..detectors.orderblock import OrderBlockDetector
from ..quarters import DAY_SHIFT, SESSIONS, WEEKDAYS, to_new_york
from ..structure import Context
from ..timeframes import Timeframe, resample_ohlcv
from .dataset import FeatureParams, unix
from .outcome import TradePlan, plan_levels, simulate_trade

FILTERS = ("all", "model", "labels")
SPLITS = {"priority": "OB priority", "swing": "Swing", "session": "Session", "weekday": "Weekday",
          "direction": "Direction"}
CSV_COLUMNS = ("time", "start", "exit", "direction", "priority", "swing", "session", "weekday", "top", "bottom",
               "entry", "stop", "target", "outcome", "r", "win_prob", "equity_r")


@dataclass(frozen=True)
class Block:
    time: pd.Timestamp  # the order block candle
    known: pd.Timestamp  # the close of the break candle: the earliest the block is tradable
    direction: str
    top: float
    bottom: float
    priority: str | None
    swing: str | None


def detect_blocks(m1: pd.DataFrame, tf: Timeframe, detector: DetectorParams) -> list[Block]:
    """Every order block of the history on ``tf``, oldest first (slow on a year of 5M: cache it)."""
    candles = resample_ohlcv(m1, tf)
    if len(candles) <= 2 * detector.swing_length + 2:
        return []
    out = [Block(lv.time, lv.confirmed_time + tf.delta, lv.kind, float(lv.top), float(lv.bottom),
                 lv.meta.get("priority"), lv.meta.get("swing"))
           for lv in OrderBlockDetector(detector).detect(Context(candles))]
    out.sort(key=lambda b: (b.known, b.time))
    return out


def _session_and_day(times: pd.DatetimeIndex, clock: str) -> tuple[list[str], list[str]]:
    shifted = to_new_york(times, clock) + DAY_SHIFT
    sessions, days = [], []
    for t in shifted:
        if pd.isna(t):
            sessions.append("?")
            days.append("?")
            continue
        minute = t.hour * 60 + t.minute
        sessions.append(SESSIONS[minute // 360])
        days.append(WEEKDAYS[t.dayofweek] if t.dayofweek < 5 else "Weekend")
    return sessions, days


def win_chances(bundle, m1: pd.DataFrame, tf: Timeframe, blocks: list[Block], max_sl: float
                ) -> dict[pd.Timestamp, tuple[float, pd.Timestamp]]:
    """Block candle -> (the model's win chance, when its features were ready)."""
    from .train import frames_for, zone_features

    if bundle is None or bundle.outcome is None:
        return {}
    p = bundle.manifest["params"]
    fr = frames_for(m1, tf, FeatureParams.of(p))
    rows, keys = [], []
    for b in blocks:
        if b.time not in fr.feats.index:
            continue
        row = fr.feats.loc[b.time]
        zone = zone_features(row, float(fr.atr.loc[b.time]), float(fr.candles.loc[b.time, "close"]), b.direction,
                             b.top, b.bottom, max_sl)
        if zone is None:
            continue
        rows.append({**row.to_dict(), **zone})
        keys.append((b.time, row["available_at"]))
    if not rows:
        return {}
    probs = bundle.outcome.predict_proba(pd.DataFrame(rows)[bundle.outcome_features])[:, 1]
    return {t: (float(pr), ready) for (t, ready), pr in zip(keys, probs)}


def _labelled(blocks: list[Block], labels: list[dict]) -> set[pd.Timestamp]:
    from .dataset import ts
    from .tags import OB_TAGS, TAGS

    yes = [(TAGS[lb["tag"]]["kind"], ts(lb["start"]), ts(lb["end"])) for lb in labels
           if lb["value"] == 1 and lb["tag"] in OB_TAGS]
    return {b.time for b in blocks if any(k == b.direction and s <= b.time <= e for k, s, e in yes)}


def summarize(trades: list[dict]) -> dict:
    """Trades, fill rate, win rate, average R, total R and max drawdown in R."""
    n = len(trades)
    filled = [t for t in trades if t["outcome"] != "untouched"]
    done = [t for t in trades if t["outcome"] in ("win", "loss")]
    wins = sum(t["outcome"] == "win" for t in done)
    rs = np.array([t["r"] for t in sorted(done, key=lambda t: t["exit_unix"])], dtype=float)
    equity = np.cumsum(rs) if len(rs) else np.array([])
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:] if len(rs) else np.array([])
    return {
        "trades": n, "filled": len(filled), "finished": len(done), "wins": int(wins),
        "fill_rate": len(filled) / n if n else None,
        "win_rate": wins / len(done) if done else None,
        "avg_r": float(rs.mean()) if len(rs) else None,
        "total_r": float(rs.sum()) if len(rs) else 0.0,
        "max_dd_r": float((peak - equity).max()) if len(rs) else 0.0,
    }


def run(m1: pd.DataFrame, tf: Timeframe, blocks: list[Block], plan: TradePlan, clock: str,
        start: pd.Timestamp | None = None, end: pd.Timestamp | None = None, filter: str = "all",
        min_win: float = 0.5, bundle=None, labels: list[dict] | None = None) -> dict:
    """Trade every block (tradable between ``start`` and ``end``) that passes ``filter``."""
    if filter not in FILTERS:
        raise ValueError(f"filter is one of {', '.join(FILTERS)}")
    picked = [b for b in blocks if (start is None or b.known >= start) and (end is None or b.known <= end)]
    chances: dict = {}
    if filter == "model":
        if bundle is None or bundle.outcome is None:
            raise ValueError("The win chance filter needs an active model with an outcome part (train one with "
                             "order block tags)")
        chances = win_chances(bundle, m1, tf, picked, plan.max_sl)
        picked = [b for b in picked if b.time in chances and chances[b.time][0] >= min_win]
    elif filter == "labels":
        mine = _labelled(picked, labels or [])
        picked = [b for b in picked if b.time in mine]

    trades = []
    for b in picked:
        levels = plan_levels(b.direction, b.top, b.bottom, plan.max_sl)
        if levels is None:
            continue
        entry, stop = levels
        begin = b.known
        if b.time in chances:
            begin = max(begin, chances[b.time][1])
        outcome, r, exit_at = simulate_trade(m1, b.direction, entry, stop, begin, plan)
        risk = abs(entry - stop)
        trades.append({
            "id": f"{tf.name}:{unix(b.time)}", "time_unix": unix(b.time), "start_unix": unix(begin),
            "exit_unix": unix(exit_at) if exit_at is not None else None,
            "direction": b.direction, "priority": b.priority or "none", "swing": b.swing or "none",
            "top": b.top, "bottom": b.bottom, "entry": entry, "stop": stop,
            "target": entry + plan.rr * risk if b.direction == "bullish" else entry - plan.rr * risk,
            "outcome": outcome, "r": r, "win_prob": chances[b.time][0] if b.time in chances else None,
        })
    if trades:
        sessions, days = _session_and_day(pd.DatetimeIndex([pd.Timestamp(t["start_unix"], unit="s") for t in trades]),
                                          clock)
        for t, s, d in zip(trades, sessions, days):
            t["session"], t["weekday"] = s, d

    equity, total = [], 0.0
    for t in sorted((t for t in trades if t["exit_unix"] is not None), key=lambda t: t["exit_unix"]):
        total += t["r"]
        t["equity_r"] = total
        equity.append([t["exit_unix"], total])
    splits = {}
    for key in SPLITS:
        groups: dict[str, list[dict]] = {}
        for t in trades:
            groups.setdefault(str(t[key]), []).append(t)
        order = {"session": SESSIONS, "weekday": (*WEEKDAYS, "Weekend"),
                 "priority": ("extreme", "middle", "none"), "swing": ("LH", "HH", "HL", "LL", "none")}.get(key, ())
        keys = sorted(groups, key=lambda k: (order.index(k) if k in order else len(order), k))
        splits[key] = [{"key": k, **summarize(groups[k])} for k in keys]
    return {"summary": summarize(trades), "splits": splits, "equity": equity, "trades": trades}


def trades_csv(trades: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    iso = lambda u: pd.Timestamp(u, unit="s").strftime("%Y-%m-%d %H:%M") if u is not None else ""  # noqa: E731
    for t in trades:
        w.writerow([iso(t["time_unix"]), iso(t["start_unix"]), iso(t["exit_unix"]), t["direction"], t["priority"],
                    t["swing"], t.get("session", ""), t.get("weekday", ""), t["top"], t["bottom"], t["entry"], t["stop"],
                    t["target"], t["outcome"], "" if t["r"] is None else t["r"],
                    "" if t["win_prob"] is None else round(t["win_prob"], 4),
                    "" if t.get("equity_r") is None else t["equity_r"]])
    return buf.getvalue()
