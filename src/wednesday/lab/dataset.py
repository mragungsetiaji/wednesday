"""Labels as data: a per-minute table for research, per-candle samples for training.

Everything here is causal where it has to be. A feature for candle ``i`` only
uses candles up to ``i + confirm`` (the candles that confirm it), and the
sample carries ``available_at``: the close of that last candle, the moment a
model could have known. Higher-timeframe context comes from candles that had
closed by then.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..timeframes import OHLCV_COLUMNS, TIMEFRAMES, Timeframe, resample_ohlcv

# Low to high: the next entry is the higher timeframe used as context.
LADDER: tuple[Timeframe, ...] = tuple(sorted(TIMEFRAMES, key=lambda tf: tf.minutes))
PREFIX = {"5M": "m5", "15M": "m15", "30M": "m30", "1H": "h1", "4H": "h4"}


def unix(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).timestamp())


def ts(u: int) -> pd.Timestamp:
    return pd.Timestamp(int(u), unit="s")


def label_matrix(times: pd.DatetimeIndex, labels: list[dict], reviewed: list[dict], tags: list[str]) -> pd.DataFrame:
    """1 (tagged), 0 (reviewed and not tagged, or marked "not") or NaN (never looked at), per candle and tag."""
    out = pd.DataFrame(np.nan, index=times, columns=list(tags))
    t = np.asarray([unix(x) for x in times]) if len(times) else np.array([], dtype=np.int64)
    for r in reviewed:
        mask = (t >= r["start"]) & (t <= r["end"])
        for tag in r["tags"]:
            if tag in out.columns:
                out.loc[mask, tag] = 0.0
    for value in (0, 1):  # positives win over a "not" on the same candle
        for lab in labels:
            if lab["value"] == value and lab["tag"] in out.columns:
                out.loc[(t >= lab["start"]) & (t <= lab["end"]), lab["tag"]] = float(value)
    return out


def minute_dataset(m1: pd.DataFrame, labels: dict[str, list[dict]], reviewed: dict[str, list[dict]],
                   tags: list[str], timeframes: tuple[Timeframe, ...] = LADDER) -> pd.DataFrame:
    """One row per minute: M1 OHLCV, then for every timeframe the candle forming at that minute
    (open so far, running high/low, current close) and one label column per tag.

    Label columns (``m5_ob_bull`` ...) mark every minute of a tagged candle: 1, 0 or empty
    (not reviewed). They are the answer, known afterwards; the OHLC columns are not.
    """
    out = m1[OHLCV_COLUMNS].copy()
    out.index.name = "time"
    for tf in sorted(timeframes, key=lambda x: x.minutes):
        p = PREFIX[tf.name]
        key = out.index.floor(tf.rule)
        grouped = m1.groupby(key)
        out[f"{p}_time"] = key
        out[f"{p}_open"] = grouped["open"].transform("first").to_numpy()
        out[f"{p}_high"] = grouped["high"].cummax().to_numpy()
        out[f"{p}_low"] = grouped["low"].cummin().to_numpy()
        out[f"{p}_close"] = m1["close"].to_numpy()
        matrix = label_matrix(pd.DatetimeIndex(key.unique()), labels.get(tf.name, []), reviewed.get(tf.name, []), tags)
        for tag in tags:
            out[f"{p}_{tag}"] = matrix[tag].reindex(key).to_numpy()
    return out


# ---- per-candle features -------------------------------------------------------

@dataclass(frozen=True)
class FeatureParams:
    lookback: int = 10  # candles before the candidate
    confirm: int = 3  # candles after it that must have closed before it is judged
    atr_length: int = 14


def _atr(c: pd.DataFrame, n: int) -> pd.Series:
    prev = c["close"].shift()
    tr = pd.concat([c["high"] - c["low"], (c["high"] - prev).abs(), (c["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def candle_features(candles: pd.DataFrame, tf: Timeframe, p: FeatureParams,
                    higher: list[tuple[Timeframe, pd.DataFrame]] | None = None) -> pd.DataFrame:
    """Shape of the candidate candle and its neighbours, in ATRs, for every candle that has
    ``p.confirm`` closed candles after it. ``higher`` holds closed candles of the next two
    timeframes up (context as of ``available_at``)."""
    c = candles[["open", "high", "low", "close"]]
    atr = _atr(c, p.atr_length).replace(0, np.nan)
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body_top, body_bot = np.maximum(o, cl), np.minimum(o, cl)
    cols: dict[str, pd.Series] = {}
    for j in range(-p.lookback, p.confirm + 1):
        s = -j  # shift(-j) brings candle i+j onto row i
        cols[f"body_{j}"] = (cl - o).shift(s) / atr
        cols[f"range_{j}"] = (h - l).shift(s) / atr
        cols[f"uwick_{j}"] = (h - body_top).shift(s) / atr
        cols[f"lwick_{j}"] = (body_bot - l).shift(s) / atr
        if j:
            cols[f"close_{j}"] = (cl.shift(s) - cl) / atr
            cols[f"high_{j}"] = (h.shift(s) - h) / atr
            cols[f"low_{j}"] = (l.shift(s) - l) / atr
    win = p.lookback + p.confirm + 1
    # How far the candidate's high/low is from the extremes of the whole window (0 = it is the extreme).
    cols["high_vs_window"] = (h.rolling(win).max().shift(-p.confirm) - h) / atr
    cols["low_vs_window"] = (l - l.rolling(win).min().shift(-p.confirm)) / atr
    cols["atr_pct"] = atr / cl * 100
    hour = pd.Series(c.index.hour + c.index.minute / 60, index=c.index)
    cols["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    cols["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    cols["weekday"] = pd.Series(c.index.weekday, index=c.index).astype(float)
    cols["tf_minutes"] = pd.Series(float(tf.minutes), index=c.index)
    feats = pd.DataFrame(cols)

    last = c.index.to_series().shift(-p.confirm)
    available = last + tf.delta
    feats = feats[available.notna()]
    feats["available_at"] = available[available.notna()]

    for n, (htf, hc) in enumerate(higher or [], start=1):
        feats = _with_context(feats, htf, hc, cl.reindex(feats.index), atr.reindex(feats.index), f"htf{n}")
    for n in range(len(higher or []) + 1, 3):  # same columns on every timeframe (4H has nothing above)
        for k in ("close", "high", "low", "body"):
            feats[f"htf{n}_{k}"] = np.nan
    return feats


def _with_context(feats: pd.DataFrame, htf: Timeframe, hc: pd.DataFrame, close: pd.Series, atr: pd.Series, name: str) -> pd.DataFrame:
    """Last higher-timeframe candle closed by ``available_at``, relative to the candidate's close."""
    if hc is None or hc.empty:
        for k in ("close", "high", "low", "body"):
            feats[f"{name}_{k}"] = np.nan
        return feats
    ctx = pd.DataFrame({
        "closed_at": hc.index + htf.delta,
        "h_close": hc["close"].to_numpy(), "h_high": hc["high"].to_numpy(),
        "h_low": hc["low"].to_numpy(), "h_body": (hc["close"] - hc["open"]).to_numpy(),
    }).sort_values("closed_at")
    left = pd.DataFrame({"available_at": feats["available_at"].to_numpy(), "row": np.arange(len(feats))})
    merged = pd.merge_asof(left.sort_values("available_at"), ctx, left_on="available_at", right_on="closed_at",
                           direction="backward").sort_values("row")
    a, cl = atr.to_numpy(), close.to_numpy()
    feats[f"{name}_close"] = (cl - merged["h_close"].to_numpy()) / a
    feats[f"{name}_high"] = (merged["h_high"].to_numpy() - cl) / a
    feats[f"{name}_low"] = (cl - merged["h_low"].to_numpy()) / a
    feats[f"{name}_body"] = merged["h_body"].to_numpy() / a
    return feats


def higher_candles(m1: pd.DataFrame, tf: Timeframe) -> list[tuple[Timeframe, pd.DataFrame]]:
    """Closed candles of the next two timeframes above ``tf`` (fewer near the top)."""
    idx = [x.name for x in LADDER].index(tf.name)
    return [(h, resample_ohlcv(m1, h)) for h in LADDER[idx + 1: idx + 3]]


def timeframe_features(m1: pd.DataFrame, tf: Timeframe, p: FeatureParams) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(closed candles, their features) for one timeframe."""
    candles = resample_ohlcv(m1, tf)
    return candles, candle_features(candles, tf, p, higher_candles(m1, tf))


FEATURE_META = ("available_at",)


def feature_names(feats: pd.DataFrame) -> list[str]:
    return [c for c in feats.columns if c not in FEATURE_META]
