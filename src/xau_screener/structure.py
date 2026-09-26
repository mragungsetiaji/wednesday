"""Market structure shared by all detectors: swings, breaks of structure, ATR.

Detectors never recompute these; they ask the :class:`Context` for a
``structure(length)``, which is computed once per swing length and cached.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Literal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Swing:
    kind: Literal["high", "low"]
    index: int  # candle index of the pivot
    price: float
    confirmed: int  # index of the candle whose close confirms the pivot (index + length)


@dataclass(frozen=True)
class Break:
    direction: Literal["bullish", "bearish"]
    index: int  # candle that closed beyond the swing
    swing: Swing  # the swing that was broken


@dataclass
class Structure:
    length: int
    swings: list[Swing]  # in confirmation order
    breaks: list[Break]  # in time order


def pivot_mask(values: np.ndarray, length: int, find_high: bool) -> np.ndarray:
    """Bars whose value is the extreme of ``length`` bars on each side (strict on the left)."""
    n = len(values)
    mask = np.zeros(n, dtype=bool)
    for i in range(length, n - length):
        window = values[i - length : i + length + 1]
        left = values[i - length : i]
        v = values[i]
        if find_high:
            mask[i] = v >= window.max() and v > left.max()
        else:
            mask[i] = v <= window.min() and v < left.min()
    return mask


def analyze_structure(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int) -> Structure:
    """Find swings and breaks of structure without look-ahead.

    A swing is only usable once ``length`` bars after it have closed. A bullish
    break is a close above the latest unbroken swing high (bearish: below the
    latest unbroken swing low); each swing can be broken once.
    """
    if length < 1:
        raise ValueError("swing length must be >= 1")
    n = len(close)
    hi_mask = pivot_mask(high, length, find_high=True)
    lo_mask = pivot_mask(low, length, find_high=False)
    swings: list[Swing] = []
    breaks: list[Break] = []
    last_high: Swing | None = None
    last_low: Swing | None = None
    for i in range(n):
        p = i - length
        if p >= 0:
            if hi_mask[p]:
                last_high = Swing("high", p, float(high[p]), i)
                swings.append(last_high)
            if lo_mask[p]:
                last_low = Swing("low", p, float(low[p]), i)
                swings.append(last_low)
        if last_high is not None and close[i] > last_high.price:
            breaks.append(Break("bullish", i, last_high))
            last_high = None
        if last_low is not None and close[i] < last_low.price:
            breaks.append(Break("bearish", i, last_low))
            last_low = None
    return Structure(length, swings, breaks)


class Context:
    """Closed candles of one timeframe plus lazily computed, shared analysis."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.times = df.index
        self.open = df["open"].to_numpy(dtype=float)
        self.high = df["high"].to_numpy(dtype=float)
        self.low = df["low"].to_numpy(dtype=float)
        self.close = df["close"].to_numpy(dtype=float)
        self._structures: dict[int, Structure] = {}

    def __len__(self) -> int:
        return len(self.close)

    def structure(self, length: int) -> Structure:
        if length not in self._structures:
            self._structures[length] = analyze_structure(self.high, self.low, self.close, length)
        return self._structures[length]

    @cached_property
    def atr(self) -> np.ndarray:
        """14-period ATR (simple mean of true range; the first bars use what is available)."""
        prev_close = np.concatenate([[self.close[0]], self.close[:-1]])
        tr = np.maximum(self.high - self.low, np.maximum(abs(self.high - prev_close), abs(self.low - prev_close)))
        return pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy()


@dataclass(frozen=True)
class Bias:
    """Direction of the latest break of structure on a timeframe."""

    direction: Literal["bullish", "bearish"]
    event: Literal["BOS", "CHoCH"]  # CHoCH = the break flipped the previous direction
    level: float  # price of the swing that was broken
    swing_time: pd.Timestamp  # when that swing printed
    break_time: pd.Timestamp  # candle that closed beyond it
    bars_ago: int  # closed candles since the break candle
    streak: int  # consecutive breaks in this direction, including this one

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "event": self.event,
            "level": self.level,
            "swing_time": self.swing_time.isoformat(),
            "break_time": self.break_time.isoformat(),
            "bars_ago": self.bars_ago,
            "streak": self.streak,
        }


def latest_bias(ctx: Context, length: int) -> Bias | None:
    """Bias from the most recent break; None until the first break in the window."""
    breaks = ctx.structure(length).breaks
    if not breaks:
        return None
    last = breaks[-1]
    streak = 1
    for prev in reversed(breaks[:-1]):
        if prev.direction != last.direction:
            break
        streak += 1
    # The first break against the previous direction is a change of character; later ones continue it.
    event = "CHoCH" if streak == 1 and len(breaks) > 1 else "BOS"
    return Bias(last.direction, event, last.swing.price, ctx.times[last.swing.index],
                ctx.times[last.index], len(ctx) - 1 - last.index, streak)
