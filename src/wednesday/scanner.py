"""Multi-timeframe scan: run every detector on every timeframe (high -> low) and
find the nearest active level above and below price for each detector."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .bias import TradeBias, setup_risk
from .detectors import DEFAULT_DETECTORS, REGISTRY, DetectorParams, build_detectors
from .detectors.orderblock import PRIORITY_RANK
from .levels import Level
from .structure import Bias, Context, SwingPoint, label_swings, latest_bias
from .timeframes import TIMEFRAMES, Timeframe, resample_ohlcv


@dataclass
class ScanConfig:
    timeframes: tuple[Timeframe, ...] = TIMEFRAMES
    lookback: int = 200  # closed candles per timeframe handed to the detectors
    detectors: tuple[str, ...] = DEFAULT_DETECTORS
    params: DetectorParams = field(default_factory=DetectorParams)
    recent_bars: int = 3  # levels that ended within this many candles are reported as events

    def required_m1_bars(self) -> int:
        """M1 history needed so every timeframe gets ``lookback`` closed candles."""
        return max(tf.minutes for tf in self.timeframes) * (self.lookback + 1)

    def to_dict(self) -> dict:
        return {
            "timeframes": [tf.name for tf in self.timeframes],
            "lookback": self.lookback,
            "detectors": [{"name": n, "title": REGISTRY[n].title} for n in self.detectors],
            "recent_bars": self.recent_bars,
            **self.params.__dict__,
        }


@dataclass
class LevelSet:
    """One detector's output on one timeframe, split relative to price."""

    active: list[Level]
    above: Level | None  # nearest active level fully above price
    below: Level | None  # nearest active level fully below price
    inside: list[Level]  # active levels containing price
    recent: list[Level]  # levels that ended within the last ``recent_bars`` candles

    def to_dict(self) -> dict:
        def one(lv):
            return lv.to_dict() if lv else None

        return {
            "active_count": len(self.active),
            "active": [lv.to_dict() for lv in self.active],
            "above": one(self.above),
            "below": one(self.below),
            "inside": [lv.to_dict() for lv in self.inside],
            "recent": [lv.to_dict() for lv in self.recent],
        }


@dataclass
class TimeframeResult:
    timeframe: Timeframe
    candles: int
    sets: dict[str, LevelSet]  # detector name -> results
    bias: Bias | None = None  # latest break of structure (external swings)
    swings: list[SwingPoint] = field(default_factory=list)  # labelled HH/LH/HL/LL


@dataclass
class ScanResult:
    time: pd.Timestamp
    price: float
    detectors: tuple[str, ...]
    results: list[TimeframeResult]  # ordered high TF -> low TF

    def nearest(self, detector: str, side: str) -> tuple[Timeframe, Level] | None:
        """Nearest level of ``detector`` across all timeframes; side is "above" or "below"."""
        cands = [(r.timeframe, getattr(r.sets[detector], side)) for r in self.results if detector in r.sets]
        cands = [(tf, lv) for tf, lv in cands if lv is not None]
        if not cands:
            return None
        if side == "above":
            return min(cands, key=lambda x: x[1].bottom - self.price)
        return min(cands, key=lambda x: self.price - x[1].top)

    def setups(self, side: str, limit: int = 5, bias: TradeBias | None = None) -> list[tuple[Timeframe, Level, float]]:
        """OB limit setups on one side ("sell": above price, "buy": below), best first.

        Extreme OBs rank before middle ones, then the nearest entry wins. On the
        side the trader's bias favours, order blocks at a lower high (sells) or
        higher low (buys) come first. Returns (timeframe, level, distance from
        price to the entry).
        """
        focus = "LH" if side == "sell" else "HL"
        prefer_swing = setup_risk(side, bias) == "on"
        want = "bearish" if side == "sell" else "bullish"
        out = []
        for r in self.results:
            obs = r.sets.get("ob")
            for lv in obs.active if obs else []:
                entry = lv.meta.get("entry")
                if lv.kind != want or entry is None:
                    continue
                dist = entry - self.price if side == "sell" else self.price - entry
                if dist > 0:  # the limit must still be on the far side of price
                    out.append((r.timeframe, lv, dist))
        out.sort(key=lambda t: (prefer_swing and t[1].meta.get("swing") != focus,
                                PRIORITY_RANK.get(t[1].meta.get("priority"), 9), t[2]))
        return out[:limit]

    def to_dict(self, bias: TradeBias | None = None) -> dict:
        """``bias`` (an active trade bias) labels each setup RISK ON / OFF / NO TRADE."""
        def near(det, side):
            hit = self.nearest(det, side)
            return {"timeframe": hit[0].name, **hit[1].to_dict()} if hit else None

        return {
            "time": self.time.isoformat(),
            "price": self.price,
            "detectors": list(self.detectors),
            "nearest": {d: {"above": near(d, "above"), "below": near(d, "below")} for d in self.detectors},
            "setups": {
                side: [{"timeframe": tf.name, "distance": dist, "risk": setup_risk(side, bias), **lv.to_dict()}
                       for tf, lv, dist in self.setups(side, bias=bias)]
                for side in ("sell", "buy")
            },
            "timeframes": [
                {
                    "timeframe": r.timeframe.name,
                    "candles": r.candles,
                    "bias": {**r.bias.to_dict(),
                             "break_close_time": (r.bias.break_time + r.timeframe.delta).isoformat()}
                    if r.bias else None,
                    "detectors": {d: s.to_dict() for d, s in r.sets.items()},
                }
                for r in self.results
            ],
        }


def split_by_price(levels: list[Level], price: float) -> tuple[Level | None, Level | None, list[Level]]:
    """Nearest level fully above price, nearest fully below, and levels containing price."""
    above = [lv for lv in levels if lv.bottom > price]
    below = [lv for lv in levels if lv.top < price]
    inside = [lv for lv in levels if lv.contains(price)]
    nearest_above = min(above, key=lambda lv: lv.bottom - price) if above else None
    nearest_below = min(below, key=lambda lv: price - lv.top) if below else None
    return nearest_above, nearest_below, inside


def scan_timeframe(m1: pd.DataFrame, tf: Timeframe, price: float, cfg: ScanConfig) -> tuple[TimeframeResult, pd.DataFrame]:
    candles = resample_ohlcv(m1, tf).tail(cfg.lookback)
    ctx = Context(candles)  # shared structure: swings/BOS are computed once for all detectors
    recent_from = candles.index[-cfg.recent_bars] if len(candles) >= cfg.recent_bars > 0 else None
    sets = {}
    for det in build_detectors(cfg.detectors, cfg.params):
        levels = det.detect(ctx) if len(candles) else []
        active = [lv for lv in levels if lv.active]
        above, below, inside = split_by_price(active, price)
        recent = [lv for lv in levels if not lv.active and recent_from is not None and lv.ended_time >= recent_from]
        sets[det.name] = LevelSet(active, above, below, inside, recent)
    bias = latest_bias(ctx, cfg.params.swing_length) if len(candles) else None
    swings = label_swings(ctx.structure(cfg.params.swing_length), ctx.times) if len(candles) else []
    return TimeframeResult(tf, len(candles), sets, bias, swings), candles


def scan(m1: pd.DataFrame, cfg: ScanConfig, price: float | None = None) -> ScanResult:
    """Scan every timeframe from high to low against the current price."""
    if m1.empty:
        raise ValueError("No M1 data to scan")
    if price is None:
        price = float(m1["close"].iloc[-1])
    ordered = sorted(cfg.timeframes, key=lambda tf: tf.minutes, reverse=True)
    results = [scan_timeframe(m1, tf, price, cfg)[0] for tf in ordered]
    return ScanResult(m1.index[-1], price, cfg.detectors, results)
