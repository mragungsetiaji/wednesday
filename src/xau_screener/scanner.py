"""Multi-timeframe order block scan: nearest active OB above and below price."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .orderblock import OrderBlock, active_order_blocks, detect_order_blocks
from .timeframes import TIMEFRAMES, Timeframe, resample_ohlcv


@dataclass
class ScanConfig:
    timeframes: tuple[Timeframe, ...] = TIMEFRAMES
    lookback: int = 200  # closed candles per timeframe to search for order blocks
    swing_length: int = 5
    zone: str = "wick"  # "wick" (full candle range) or "body"
    mitigation: str = "close"  # "close" or "wick"

    def required_m1_bars(self) -> int:
        """M1 history needed so every timeframe gets ``lookback`` closed candles."""
        return max(tf.minutes for tf in self.timeframes) * (self.lookback + 1)


@dataclass
class TimeframeResult:
    timeframe: Timeframe
    candles: int
    active: list[OrderBlock]
    above: OrderBlock | None
    below: OrderBlock | None
    inside: list[OrderBlock] = field(default_factory=list)


@dataclass
class ScanResult:
    time: pd.Timestamp
    price: float
    results: list[TimeframeResult]  # ordered high TF -> low TF

    def nearest_above(self) -> tuple[Timeframe, OrderBlock] | None:
        cands = [(r.timeframe, r.above) for r in self.results if r.above]
        return min(cands, key=lambda x: x[1].bottom - self.price) if cands else None

    def nearest_below(self) -> tuple[Timeframe, OrderBlock] | None:
        cands = [(r.timeframe, r.below) for r in self.results if r.below]
        return min(cands, key=lambda x: self.price - x[1].top) if cands else None

    def to_dict(self) -> dict:
        def ob(o):
            return o.to_dict() if o else None

        return {
            "time": self.time.isoformat(),
            "price": self.price,
            "timeframes": [
                {
                    "timeframe": r.timeframe.name,
                    "candles": r.candles,
                    "active_count": len(r.active),
                    "above": ob(r.above),
                    "below": ob(r.below),
                    "inside": [o.to_dict() for o in r.inside],
                }
                for r in self.results
            ],
        }


def split_by_price(blocks: list[OrderBlock], price: float) -> tuple[OrderBlock | None, OrderBlock | None, list[OrderBlock]]:
    """Nearest block fully above price, nearest fully below, and blocks containing price."""
    above = [ob for ob in blocks if ob.bottom > price]
    below = [ob for ob in blocks if ob.top < price]
    inside = [ob for ob in blocks if ob.contains(price)]
    nearest_above = min(above, key=lambda ob: ob.bottom - price) if above else None
    nearest_below = min(below, key=lambda ob: price - ob.top) if below else None
    return nearest_above, nearest_below, inside


def scan_timeframe(m1: pd.DataFrame, tf: Timeframe, price: float, cfg: ScanConfig) -> TimeframeResult:
    candles = resample_ohlcv(m1, tf).tail(cfg.lookback)
    blocks = detect_order_blocks(candles, cfg.swing_length, cfg.zone, cfg.mitigation)
    active = active_order_blocks(blocks)
    above, below, inside = split_by_price(active, price)
    return TimeframeResult(tf, len(candles), active, above, below, inside)


def scan(m1: pd.DataFrame, cfg: ScanConfig, price: float | None = None) -> ScanResult:
    """Scan every timeframe from high to low against the current price."""
    if m1.empty:
        raise ValueError("No M1 data to scan")
    if price is None:
        price = float(m1["close"].iloc[-1])
    ordered = sorted(cfg.timeframes, key=lambda tf: tf.minutes, reverse=True)
    results = [scan_timeframe(m1, tf, price, cfg) for tf in ordered]
    return ScanResult(m1.index[-1], price, results)
