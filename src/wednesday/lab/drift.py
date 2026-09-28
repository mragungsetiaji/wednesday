"""Is the active model still doing what its training scores said? A live scorecard since the
model was turned on, from three angles:

* **Reviews**: the share of its blocks you marked valid, against its held-out precision.
* **Market**: the traded result of its order block calls (the limit plan, simulated like a
  review), against the held-out average R.
* **Inputs**: volatility (ATR %) and candle bodies on the recent candles, against the
  training history's quantiles stored in the manifest.

It only ever flags, quietly, and only after enough samples: it never turns a model off.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..timeframes import TIMEFRAMES_BY_NAME
from .dataset import FeatureParams, ts, unix
from .outcome import TradePlan, plan_levels, simulate
from .tags import OB_TAGS, TAGS
from .train import INPUTS, _inputs_of, frames_for, predict_blocks

MIN_SAMPLES = 20  # reviews or finished trades before any warning
LIVE_CANDLES = 300  # recent candles per timeframe compared with the training inputs
MIN_CANDLES = 50
SHIFT = 1.5  # an input median this many times higher (or lower) than in training is flagged
MAX_CALLS_BACK = 2000  # candles per timeframe searched for the model's calls


def _band(expected: float, spread: float, n: int) -> float:
    """Two standard errors below what held-out data promised."""
    return expected - 2 * spread / math.sqrt(n)


def review_score(manifest: dict, reviews: list[dict]) -> dict:
    n = len(reviews)
    valid = sum(r["verdict"] == "valid" for r in reviews)
    precisions = [manifest["tags"].get(r["tag"], {}).get("precision") for r in reviews]
    known = [p for p in precisions if p is not None]
    expected = float(np.mean(known)) if known else None
    out = {"n": n, "valid": valid, "rate": valid / n if n else None, "expected": expected, "low": False}
    if n >= MIN_SAMPLES and expected is not None:
        out["low"] = out["rate"] < _band(expected, math.sqrt(expected * (1 - expected)), n)
    return out


def market_score(bundle, m1: pd.DataFrame | None, since_unix: int | None) -> dict:
    """Simulate the limit plan on every order block the model called since ``since_unix``."""
    expected = ((bundle.manifest.get("outcome") or {}).get("avg_r_all"))
    out = {"calls": 0, "finished": 0, "wins": 0, "avg_r": None, "expected": expected, "low": False}
    if m1 is None or m1.empty or since_unix is None or not any(t in OB_TAGS for t in bundle.models):
        return out
    p = bundle.manifest["params"]
    plan = TradePlan(rr=float(p.get("rr", 2.0)), horizon_minutes=int(p.get("horizon_hours", 72)) * 60,
                     max_sl=float(p.get("max_sl", 3.0)))
    rs = []
    for name in bundle.manifest["timeframes"]:
        tf = TIMEFRAMES_BY_NAME[name]
        count = int((m1.index >= ts(since_unix)).sum()) // tf.minutes + 1
        if count <= 1:
            continue
        for b in predict_blocks(bundle, m1, tf, limit=min(count + int(p["confirm"]), MAX_CALLS_BACK)):
            if b["tag"] not in OB_TAGS or b["available_unix"] < since_unix:
                continue
            out["calls"] += 1
            kind = TAGS[b["tag"]]["kind"]
            levels = plan_levels(kind, b["top"], b["bottom"], plan.max_sl)
            if not levels:
                continue
            result, r = simulate(m1, kind, *levels, ts(b["available_unix"]), plan)
            if result in ("win", "loss"):
                rs.append(r)
                out["wins"] += result == "win"
    out["finished"] = len(rs)
    if rs:
        out["avg_r"] = float(np.mean(rs))
    if len(rs) >= MIN_SAMPLES and expected is not None:
        out["low"] = out["avg_r"] < _band(expected, float(np.std(rs, ddof=1)), len(rs))
    return out


def input_shift(bundle, m1: pd.DataFrame | None) -> list[dict] | None:
    """Recent medians of the watched inputs against training; None for files without quantiles."""
    trained = bundle.manifest.get("inputs")
    if not trained:
        return None
    if m1 is None or m1.empty:
        return []
    p = bundle.manifest["params"]
    fp = FeatureParams(lookback=int(p["lookback"]), confirm=int(p["confirm"]))
    out = []
    for name, q in trained.items():
        tf = TIMEFRAMES_BY_NAME.get(name)
        if tf is None:
            continue
        need = (LIVE_CANDLES + fp.lookback + fp.confirm + 20) * tf.minutes + 3 * 240
        feats = frames_for(m1.tail(need), tf, fp).feats.tail(LIVE_CANDLES)
        if len(feats) < MIN_CANDLES:
            continue
        for key, live in _inputs_of(feats).items():
            if key not in q or not q[key][1]:
                continue
            ratio = float(live.median()) / q[key][1]
            out.append({"timeframe": name, "input": key, "title": INPUTS[key], "trained": q[key],
                        "live": float(live.median()), "ratio": ratio,
                        "shifted": ratio >= SHIFT or ratio <= 1 / SHIFT})
    return out


def scorecard(bundle, reviews: list[dict], m1: pd.DataFrame | None, since: str, since_unix: int | None) -> dict:
    """The active model's live results since ``since`` (UTC ISO; ``since_unix`` in the feed clock)."""
    mine = [r for r in reviews if r["model_id"] == bundle.id and r["created_at"] >= since]
    rev = review_score(bundle.manifest, mine)
    market = market_score(bundle, m1, since_unix)
    inputs = input_shift(bundle, m1)
    day = since[:10]
    warnings = []
    if rev["low"]:
        warnings.append(f"Only {rev['rate']:.0%} of the {rev['n']} blocks you reviewed since {day} were valid; "
                        f"on held-out data about {rev['expected']:.0%} were.")
    if market["low"]:
        warnings.append(f"Its order block calls since {day} averaged {market['avg_r']:+.2f}R over "
                        f"{market['finished']} finished trades; held-out trades averaged {market['expected']:+.2f}R.")
    for s in inputs or []:
        if s["shifted"]:
            warnings.append(f"{s['timeframe']} {s['title']} is {s['ratio']:.1f}× what the model was trained on.")
    return {"model_id": bundle.id, "since": since, "since_unix": since_unix, "min_samples": MIN_SAMPLES,
            "reviews": rev, "market": market, "inputs": inputs, "warnings": warnings,
            "latest_unix": unix(m1.index[-1]) if m1 is not None and len(m1) else None}
