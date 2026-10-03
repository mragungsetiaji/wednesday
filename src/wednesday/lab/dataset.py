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

from ..quarters import DAY_SHIFT, to_new_york, utc_index_to_feed
from ..structure import analyze_structure
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

# Optional feature families, switched on per model in the Train form. A model's manifest lists the
# ones it was trained with and prediction builds exactly those (none for models made before them).
FAMILIES = {
    "quarters": "Session and quarter: Tokyo / London / NY AM / NY PM, the 90-minute quarter, the trading "
                "weekday, and how far price has moved since the week and the session opened (New York time)",
    "structure": "Structure: the latest break on this timeframe and the one above (direction, BOS or CHoCH, "
                 "candles since), the last swing label, and the distance to the unbroken swing high and low",
    "liquidity": "Liquidity: a swing high or low swept by one of the last 5 candles, and the distance to the "
                 "nearest equal highs and lows",
    "news": "News: minutes to the next and since the last high-impact USD release, from the stored calendar",
    "bias": "Bias: the direction you had set (bullish, bearish or neutral) when the candle was judged, from "
            "the bias history kept since this version",
}
STRUCTURE_LENGTH = 5  # swing length for the structure and liquidity families (the detectors' default)
SWEEP_CANDLES = 5  # how far back a sweep counts
EQ_TOLERANCE = 0.1  # equal highs / lows: within this many ATRs (the liquidity detector's default)

# High-impact release times (unix, UTC) and the stored calendar's first and last, for the news
# family. Set by the Lab from its database (use_news); None leaves the news columns empty.
_news_source = None


_bias_source = None


def use_bias(source) -> None:
    """``source()`` -> [{"set_at", "direction", "expires_at"}] oldest first (ISO UTC), or None to clear."""
    global _bias_source
    _bias_source = source


def use_news(source) -> None:
    """``source()`` -> (sorted release times, unix UTC; first stored; last stored), or None to clear."""
    global _news_source
    _news_source = source


@dataclass(frozen=True)
class FeatureParams:
    lookback: int = 10  # candles before the candidate
    confirm: int = 3  # candles after it that must have closed before it is judged
    atr_length: int = 14
    families: tuple[str, ...] = ()  # optional families (FAMILIES)
    clock: str = "UTC"  # the bars' clock, for the families in New York time

    @classmethod
    def of(cls, params: dict) -> FeatureParams:
        """From a model manifest's ``params``."""
        return cls(lookback=int(params["lookback"]), confirm=int(params["confirm"]),
                   families=tuple(f for f in params.get("families") or () if f in FAMILIES),
                   clock=str(params.get("clock") or "UTC"))


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
    if "quarters" in p.families:
        cols.update(_quarter_features(c, atr, p.clock))
    if "news" in p.families:
        cols.update(_news_features(c, p.clock))
    # Structure and liquidity are known at the close of the last confirming candle: state at k = i + confirm.
    if "structure" in p.families or "liquidity" in p.families:
        state = _structure_state(c, atr, STRUCTURE_LENGTH).shift(-p.confirm)
        keep = [k for k in state.columns if k.startswith("s_")] if "structure" in p.families else []
        keep += [k for k in state.columns if k.startswith("l_")] if "liquidity" in p.families else []
        for k in keep:
            cols[k] = state[k]
        if "structure" in p.families:  # distances from the candidate's close
            cols["s_dist_high"] = (state["hi"] - cl) / atr
            cols["s_dist_low"] = (cl - state["lo"]) / atr
        if "liquidity" in p.families:
            cols["l_dist_eq_high"] = (state["eq_hi"] - cl) / atr
            cols["l_dist_eq_low"] = (cl - state["eq_lo"]) / atr
    feats = pd.DataFrame(cols)

    last = c.index.to_series().shift(-p.confirm)
    available = last + tf.delta
    feats = feats[available.notna()]
    feats["available_at"] = available[available.notna()]
    if "bias" in p.families:
        feats["b_bias"] = _bias_at(feats["available_at"], p.clock)

    for n, (htf, hc) in enumerate(higher or [], start=1):
        feats = _with_context(feats, htf, hc, cl.reindex(feats.index), atr.reindex(feats.index), f"htf{n}")
    if "structure" in p.families:
        feats = _htf_structure(feats, (higher or [None])[0], p)
    for n in range(len(higher or []) + 1, 3):  # same columns on every timeframe (4H has nothing above)
        for k in ("close", "high", "low", "body"):
            feats[f"htf{n}_{k}"] = np.nan
    return feats


def _structure_state(c: pd.DataFrame, atr: pd.Series, length: int) -> pd.DataFrame:
    """Per candle, what structure and liquidity looked like once it had closed (nothing later):
    the latest break, the last swing's label, the unbroken swing high and low, a recent sweep, and
    the nearest equal highs and lows still standing."""
    h, l, cl = (c[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    a = atr.to_numpy(dtype=float)
    st = analyze_structure(h, l, cl, length)
    n = len(c)
    swings_at: dict[int, list] = {}
    for sw in st.swings:
        swings_at.setdefault(sw.confirmed, []).append(sw)
    breaks_at: dict[int, list] = {}
    for b in st.breaks:
        breaks_at.setdefault(b.index, []).append(b)
    out = {k: np.full(n, np.nan) for k in ("s_break_dir", "s_break_choch", "s_since_break", "s_swing_label", "hi", "lo",
                                            "l_swept_high", "l_swept_low", "eq_hi", "eq_lo")}
    last_dir, last_break = 0, None
    label, prev = 0.0, {"high": None, "low": None}
    act_hi = act_lo = np.nan
    pools = {"high": [], "low": []}  # confirmed swings no wick has taken out yet
    swept_high = swept_low = -10**9  # index of the last sweep
    for i in range(n):
        # A sweep: the wick goes past the standing swing, the close comes back.
        if not np.isnan(act_hi) and h[i] > act_hi and cl[i] <= act_hi:
            swept_high = i
        if not np.isnan(act_lo) and l[i] < act_lo and cl[i] >= act_lo:
            swept_low = i
        pools["high"] = [x for x in pools["high"] if h[i] <= x]
        pools["low"] = [x for x in pools["low"] if l[i] >= x]
        for sw in swings_at.get(i, ()):
            ref = prev[sw.kind]
            if ref is not None:
                label = {("high", True): 2.0, ("high", False): -1.0, ("low", True): 1.0, ("low", False): -2.0}[
                    (sw.kind, sw.price > ref)]  # HH 2, HL 1, LH -1, LL -2
            prev[sw.kind] = sw.price
            if sw.kind == "high":
                act_hi = sw.price
                pools["high"] = (pools["high"] + [sw.price])[-10:]
            else:
                act_lo = sw.price
                pools["low"] = (pools["low"] + [sw.price])[-10:]
        for b in breaks_at.get(i, ()):
            d = 1.0 if b.direction == "bullish" else -1.0
            out["s_break_choch"][i] = float(last_dir != 0 and d != last_dir)
            last_dir, last_break = d, i
            if d > 0:
                act_hi = np.nan
            else:
                act_lo = np.nan
        if last_break is not None:
            out["s_break_dir"][i] = last_dir
            out["s_since_break"][i] = i - last_break
            if np.isnan(out["s_break_choch"][i]):
                out["s_break_choch"][i] = out["s_break_choch"][i - 1] if i else 0.0
        out["s_swing_label"][i] = label
        out["hi"][i], out["lo"][i] = act_hi, act_lo
        out["l_swept_high"][i] = float(i - swept_high < SWEEP_CANDLES)
        out["l_swept_low"][i] = float(i - swept_low < SWEEP_CANDLES)
        tol = EQ_TOLERANCE * a[i] if not np.isnan(a[i]) else 0.0
        out["eq_hi"][i] = _nearest_pool(pools["high"], tol, cl[i], above=True)
        out["eq_lo"][i] = _nearest_pool(pools["low"], tol, cl[i], above=False)
    return pd.DataFrame(out, index=c.index)


def _nearest_pool(levels: list[float], tol: float, close: float, above: bool) -> float:
    """The nearest level where two standing swings sit within ``tol`` of each other (equal highs or lows)."""
    best = np.nan
    for i, x in enumerate(levels):
        for y in levels[i + 1:]:
            if abs(x - y) <= tol:
                lvl = max(x, y) if above else min(x, y)
                if (lvl >= close) if above else (lvl <= close):
                    if np.isnan(best) or abs(lvl - close) < abs(best - close):
                        best = lvl
    return best


def _htf_structure(feats: pd.DataFrame, higher: tuple[Timeframe, pd.DataFrame] | None, p: FeatureParams) -> pd.DataFrame:
    """The next timeframe up's latest break, as of its last candle closed by ``available_at``."""
    cols = ("s_htf_break_dir", "s_htf_since_break")
    if higher is None or higher[1] is None or higher[1].empty:
        for k in cols:
            feats[k] = np.nan
        return feats
    htf, hc = higher
    state = _structure_state(hc, _atr(hc[["open", "high", "low", "close"]], p.atr_length), STRUCTURE_LENGTH)
    ctx = pd.DataFrame({"closed_at": hc.index + htf.delta, "d": state["s_break_dir"].to_numpy(),
                        "since": state["s_since_break"].to_numpy()}).sort_values("closed_at")
    left = pd.DataFrame({"available_at": feats["available_at"].to_numpy(), "row": np.arange(len(feats))})
    merged = pd.merge_asof(left.sort_values("available_at"), ctx, left_on="available_at", right_on="closed_at",
                           direction="backward").sort_values("row")
    feats["s_htf_break_dir"] = merged["d"].to_numpy()
    feats["s_htf_since_break"] = merged["since"].to_numpy()
    return feats


_DIRECTION = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


def _bias_at(when: pd.Series, clock: str) -> np.ndarray:
    """The bias in force at each time (feed clock): +1 bullish, -1 bearish, 0 neutral; empty when none
    was set, it had expired, or the time is before the bias history starts."""
    rows = _bias_source() if _bias_source else None
    out = np.full(len(when), np.nan)
    if not rows:
        return out
    from ..quarters import to_utc

    t = to_utc(pd.DatetimeIndex(when), clock).as_unit("ns").asi8
    set_at = pd.to_datetime([r["set_at"] for r in rows], utc=True, format="ISO8601").as_unit("ns").asi8
    i = np.searchsorted(set_at, t, side="right") - 1
    for j, k in enumerate(i):
        if k < 0:
            continue
        r = rows[k]
        if r["direction"] is None:
            continue
        if r["expires_at"] and t[j] >= pd.Timestamp(r["expires_at"]).as_unit("ns").value:
            continue
        out[j] = _DIRECTION.get(r["direction"], np.nan)
    return out


def _news_features(c: pd.DataFrame, clock: str) -> dict[str, pd.Series]:
    """Minutes from the candle's open to the next high-impact release and since the last one. Release
    times are published ahead, so the next one is known; outside the stored calendar, unknown."""
    empty = {k: pd.Series(np.nan, index=c.index) for k in ("n_to_next", "n_since_last")}
    got = _news_source() if _news_source else None
    if not got or not len(got[0]):
        return empty
    times, first, last = got
    secs = lambda ix: pd.DatetimeIndex(ix).as_unit("ns").asi8 // 10**9  # noqa: E731 - any resolution in
    rel = secs(utc_index_to_feed(pd.to_datetime(np.asarray(times), unit="s", utc=True), clock))
    t = secs(c.index)
    i = np.searchsorted(rel, t, side="left")
    nxt = np.where(i < len(rel), rel[np.minimum(i, len(rel) - 1)] - t, np.nan) / 60
    prv = np.where(i > 0, t - rel[np.maximum(i - 1, 0)], np.nan) / 60
    lo = secs(utc_index_to_feed(pd.to_datetime([first], unit="s", utc=True), clock))[0] - 7 * 86400
    hi = secs(utc_index_to_feed(pd.to_datetime([last], unit="s", utc=True), clock))[0] + 7 * 86400
    covered = (t >= lo) & (t <= hi)
    return {"n_to_next": pd.Series(np.where(covered, nxt, np.nan), index=c.index),
            "n_since_last": pd.Series(np.where(covered, prv, np.nan), index=c.index)}


def _quarter_features(c: pd.DataFrame, atr: pd.Series, clock: str) -> dict[str, pd.Series]:
    """Where the candle sits in the quarterly cycle, from its open time in New York, and its close
    against the week's and the session's open. Causal: both opens are at or before the candle."""
    shifted = to_new_york(c.index, clock) + DAY_SHIFT  # 18:00 New York starts the trading day
    minutes = shifted.hour * 60 + shifted.minute
    day = shifted.normalize()
    week = day - pd.to_timedelta(day.weekday, unit="D")
    session = minutes // 360  # Tokyo, London, NY AM, NY PM
    idx = c.index
    open_ = c["open"]
    week_open = open_.groupby(np.asarray(week)).transform("first")
    session_open = open_.groupby([np.asarray(day), np.asarray(session)]).transform("first")
    return {
        "q_session": pd.Series(session.astype(float), index=idx),
        "q_q90": pd.Series(((minutes % 360) // 90).astype(float), index=idx),
        "q_weekday": pd.Series(day.weekday.astype(float), index=idx),
        "q_week_move": (c["close"] - week_open) / atr,
        "q_session_move": (c["close"] - session_open) / atr,
    }


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
