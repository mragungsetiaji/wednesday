"""Why a model calls a candle: permutation importance on the held-out part, and the
readable names of features and their families (one place, used by the API and the UI).

Importance is the drop in held-out AUC when a feature (or a whole family) is shuffled
across the held-out candles: a feature the model leans on costs a lot when scrambled.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

FAMILIES = {
    "candle": "The candle",
    "before": "Candles before",
    "confirm": "Confirming candles",
    "window": "Window high / low",
    "volatility": "Volatility",
    "time": "Time of day and week",
    "timeframe": "Timeframe",
    "htf": "Higher timeframes",
    "zone": "Order block zone",
    "quarters": "Session and quarter",
    "structure": "Structure",
    "liquidity": "Liquidity",
    "news": "News",
}
TOP_FEATURES = 15
REPEATS = 3

_SHAPE = {"body": "body", "range": "range", "uwick": "upper wick", "lwick": "lower wick",
          "close": "close vs candle", "high": "high vs candle", "low": "low vs candle"}
_CANDLE = re.compile(r"^(body|range|uwick|lwick|close|high|low)_(-?\d+)$")
_HTF = re.compile(r"^htf(\d)_(close|high|low|body)$")
_NAMED = {
    "high_vs_window": ("window", "high below the window's high"),
    "low_vs_window": ("window", "low above the window's low"),
    "atr_pct": ("volatility", "ATR as % of price"),
    "hour_sin": ("time", "hour of day (sin)"),
    "hour_cos": ("time", "hour of day (cos)"),
    "weekday": ("time", "weekday"),
    "tf_minutes": ("timeframe", "timeframe"),
    "zone_dir": ("zone", "zone direction"),
    "zone_body": ("zone", "zone height"),
    "zone_risk": ("zone", "stop distance"),
    "zone_capped": ("zone", "stop capped"),
    "zone_entry": ("zone", "entry vs close"),
    "q_session": ("quarters", "session (Tokyo to NY PM)"),
    "q_q90": ("quarters", "90-minute quarter of the session"),
    "q_weekday": ("quarters", "trading weekday"),
    "q_week_move": ("quarters", "move since the week opened"),
    "q_session_move": ("quarters", "move since the session opened"),
    "s_break_dir": ("structure", "latest break: up or down"),
    "s_break_choch": ("structure", "latest break was a change of character"),
    "s_since_break": ("structure", "candles since the latest break"),
    "s_swing_label": ("structure", "last swing (HH, HL, LH, LL)"),
    "s_dist_high": ("structure", "distance to the unbroken swing high"),
    "s_dist_low": ("structure", "distance to the unbroken swing low"),
    "s_htf_break_dir": ("structure", "next timeframe up: latest break"),
    "s_htf_since_break": ("structure", "next timeframe up: candles since its break"),
    "l_swept_high": ("liquidity", "a swing high swept in the last 5 candles"),
    "l_swept_low": ("liquidity", "a swing low swept in the last 5 candles"),
    "l_dist_eq_high": ("liquidity", "distance to equal highs"),
    "l_dist_eq_low": ("liquidity", "distance to equal lows"),
    "n_to_next": ("news", "minutes to the next high-impact release"),
    "n_since_last": ("news", "minutes since the last high-impact release"),
}
_HTF_WHAT = {"close": "close vs candle", "high": "high vs candle", "low": "low vs candle", "body": "body"}


def describe(name: str) -> tuple[str, str]:
    """(family id, readable label) of a feature column."""
    if name in _NAMED:
        return _NAMED[name]
    if m := _CANDLE.match(name):
        shape, j = _SHAPE[m[1]], int(m[2])
        if j == 0:
            return "candle", f"candle {shape}"
        if j < 0:
            return "before", f"{-j} candle{'s' if j < -1 else ''} before: {shape}"
        return "confirm", f"confirming candle {j}: {shape}"
    if m := _HTF.match(name):
        return "htf", f"{'next' if m[1] == '1' else 'second'} timeframe up: {_HTF_WHAT[m[2]]}"
    return "other", name


def medians(X: pd.DataFrame) -> dict[str, float]:
    """Training median of every feature (NaN-free: a column that is all missing stays missing)."""
    return {c: float(v) for c, v in X.median(numeric_only=True).items() if pd.notna(v)}


def contributions(model, X: pd.DataFrame, med: dict[str, float], top: int = 3) -> list[list[dict]]:
    """Per row of ``X``: the ``top`` feature families that moved its probability most, as the change
    in probability when that family is set back to its training median (positive: it pushed the
    call up). A cheap local explanation; one predict call per family for all rows at once."""
    if X.empty or not med:
        return [[] for _ in range(len(X))]
    base = model.predict_proba(X)[:, 1]
    by_family: dict[str, list[str]] = {}
    for c in X.columns:
        if c in med:
            by_family.setdefault(describe(c)[0], []).append(c)
    deltas = {}
    for fam, cols in by_family.items():
        neutral = X.copy()
        for c in cols:
            neutral[c] = med[c]
        deltas[fam] = base - model.predict_proba(neutral)[:, 1]
    out = []
    for i in range(len(X)):
        ranked = sorted(deltas, key=lambda f: -abs(deltas[f][i]))[:top]
        out.append([{"id": f, "title": FAMILIES.get(f, "Other"), "delta": float(deltas[f][i])} for f in ranked])
    return out


def _auc(model, X: pd.DataFrame, y: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, model.predict_proba(X)[:, 1]))


def _drop(model, X: pd.DataFrame, y: np.ndarray, cols: list[str], base: float, rng: np.random.Generator) -> float:
    drops = []
    for _ in range(REPEATS):
        shuffled = X.copy()
        order = rng.permutation(len(X))
        shuffled[cols] = X[cols].to_numpy()[order]
        drops.append(base - _auc(model, shuffled, y))
    return float(np.mean(drops))


def importance(model, X: pd.DataFrame, y: np.ndarray) -> dict | None:
    """Held-out AUC drop per family and for the top features; None without both classes."""
    if len(X) < 10 or len(np.unique(y)) < 2:
        return None
    rng = np.random.default_rng(0)
    base = _auc(model, X, y)
    by_family: dict[str, list[str]] = {}
    for c in X.columns:
        by_family.setdefault(describe(c)[0], []).append(c)
    families = [{"id": f, "title": FAMILIES.get(f, "Other"), "drop": _drop(model, X, y, cols, base, rng)}
                for f, cols in by_family.items()]
    features = [{"name": c, "label": describe(c)[1], "drop": _drop(model, X, y, [c], base, rng)} for c in X.columns]
    families.sort(key=lambda f: -f["drop"])
    features.sort(key=lambda f: -f["drop"])
    return {"metric": "auc", "base": base, "families": families, "features": features[:TOP_FEATURES]}
