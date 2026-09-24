"""Order block detection.

Definition used (Smart Money Concepts style):

1. Swing highs / lows are pivots: a bar whose high (low) is the extreme of the
   ``swing_length`` bars on each side. A pivot is only known ``swing_length``
   bars after it prints, so detection never looks into the future.
2. A **bullish break of structure** happens when a candle *closes* above the
   latest unbroken swing high. The **bullish order block** is the candle with
   the lowest low between that swing high and the breakout candle - the last
   sell-off candle before the impulsive move up (demand zone).
3. A **bearish break of structure** is the mirror: a close below the latest
   unbroken swing low. The **bearish order block** is the candle with the
   highest high in that leg (supply zone).
4. An order block is **mitigated** (invalidated) once price closes through it:
   a close below a bullish OB's bottom or above a bearish OB's top
   (``mitigation="wick"`` uses the low/high instead of the close).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import pandas as pd

Kind = Literal["bullish", "bearish"]


@dataclass
class OrderBlock:
    kind: Kind
    top: float
    bottom: float
    time: pd.Timestamp  # open time of the order block candle
    break_time: pd.Timestamp  # open time of the candle that broke structure
    touches: int = 0  # candles that traded back into the zone after the break
    mitigated_time: pd.Timestamp | None = None

    @property
    def mitigated(self) -> bool:
        return self.mitigated_time is not None

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("time", "break_time", "mitigated_time"):
            d[key] = d[key].isoformat() if d[key] is not None else None
        return d


def _pivots(values: np.ndarray, length: int, find_high: bool) -> np.ndarray:
    """Boolean mask of pivot bars (extreme within ``length`` bars each side)."""
    n = len(values)
    mask = np.zeros(n, dtype=bool)
    for i in range(length, n - length):
        window = values[i - length : i + length + 1]
        v = values[i]
        if find_high:
            # Strictly higher than the left side so equal highs don't create duplicate pivots.
            if v >= window.max() and v > values[i - length : i].max():
                mask[i] = True
        else:
            if v <= window.min() and v < values[i - length : i].min():
                mask[i] = True
    return mask


def detect_order_blocks(
    df: pd.DataFrame,
    swing_length: int = 5,
    zone: Literal["wick", "body"] = "wick",
    mitigation: Literal["close", "wick"] = "close",
) -> list[OrderBlock]:
    """Detect every order block in ``df`` (closed OHLC candles, oldest first).

    Returns all order blocks, mitigated or not, in formation order.
    """
    if swing_length < 1:
        raise ValueError("swing_length must be >= 1")
    n = len(df)
    if n < 2 * swing_length + 2:
        return []

    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    times = df.index

    swing_high_mask = _pivots(h, swing_length, find_high=True)
    swing_low_mask = _pivots(l, swing_length, find_high=False)

    def zone_bounds(i: int) -> tuple[float, float]:
        if zone == "body":
            return max(o[i], c[i]), min(o[i], c[i])
        return h[i], l[i]

    blocks: list[tuple[int, OrderBlock]] = []  # (break index, block)
    swing_high: int | None = None  # index of latest unbroken swing high
    swing_low: int | None = None

    for i in range(n):
        # A pivot at p is confirmed once bar p + swing_length has closed.
        p = i - swing_length
        if p >= 0:
            if swing_high_mask[p]:
                swing_high = p
            if swing_low_mask[p]:
                swing_low = p

        if swing_high is not None and c[i] > h[swing_high]:
            leg = range(swing_high, i)
            ob_idx = min(leg, key=lambda k: (l[k], -k)) if len(leg) else i
            top, bottom = zone_bounds(ob_idx)
            blocks.append((i, OrderBlock("bullish", top, bottom, times[ob_idx], times[i])))
            swing_high = None

        if swing_low is not None and c[i] < l[swing_low]:
            leg = range(swing_low, i)
            ob_idx = max(leg, key=lambda k: (h[k], k)) if len(leg) else i
            top, bottom = zone_bounds(ob_idx)
            blocks.append((i, OrderBlock("bearish", top, bottom, times[ob_idx], times[i])))
            swing_low = None

    # Track touches and mitigation on candles after the break.
    for break_idx, ob in blocks:
        for j in range(break_idx + 1, n):
            if ob.kind == "bullish":
                breach = (c[j] if mitigation == "close" else l[j]) < ob.bottom
                touched = l[j] <= ob.top
            else:
                breach = (c[j] if mitigation == "close" else h[j]) > ob.top
                touched = h[j] >= ob.bottom
            if breach:
                ob.mitigated_time = times[j]
                break
            if touched:
                ob.touches += 1

    return [ob for _, ob in blocks]


def active_order_blocks(blocks: list[OrderBlock]) -> list[OrderBlock]:
    return [ob for ob in blocks if not ob.mitigated]
