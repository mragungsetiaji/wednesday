"""Market structure shared by all detectors: swings, breaks of structure, ATR.

Detectors never recompute these; they ask the :class:`Context` for a
``structure(length)``, which is computed once per swing length and cached.

Like the detectors, this works on plain arrays and the candles' open times, without pandas
(#96): every step is a loop or an array operation that reads the same in another language.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Literal

import numpy as np

from .levels import unix

ATR_LENGTH = 14


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


@dataclass(frozen=True)
class SwingPoint:
    """A confirmed swing with its structure label against the previous swing of the same kind."""

    kind: Literal["high", "low"]
    time: datetime
    price: float
    label: Literal["HH", "LH", "HL", "LL"] | None  # None for the first swing of its kind

    def to_dict(self) -> dict:
        return {"kind": self.kind, "time": self.time.isoformat(), "time_unix": unix(self.time),
                "price": self.price, "label": self.label}


def label_swings(structure: Structure, times: Sequence[datetime]) -> list[SwingPoint]:
    """HH/LH for swing highs, HL/LL for swing lows, each against the previous one of its kind."""
    prev: dict[str, float] = {}
    out = []
    for s in sorted(structure.swings, key=lambda s: s.index):
        ref = prev.get(s.kind)
        if ref is None:
            label = None
        elif s.kind == "high":
            label = "HH" if s.price > ref else "LH"
        else:
            label = "HL" if s.price > ref else "LL"
        prev[s.kind] = s.price
        out.append(SwingPoint(s.kind, times[s.index], s.price, label))
    return out


class Context:
    """Closed candles of one timeframe plus lazily computed, shared analysis.

    ``candles`` is anything with an index of open times and open / high / low / close columns
    (the scanner passes a DataFrame); :meth:`from_arrays` builds one from plain sequences.
    """

    def __init__(self, candles):
        self.times: Sequence[datetime] = candles.index  # open time of each candle, on the feed's clock
        self.open = np.asarray(candles["open"], dtype=float)
        self.high = np.asarray(candles["high"], dtype=float)
        self.low = np.asarray(candles["low"], dtype=float)
        self.close = np.asarray(candles["close"], dtype=float)
        self._structures: dict[int, Structure] = {}

    @classmethod
    def from_arrays(cls, times: Sequence[datetime], open, high, low, close) -> Context:
        return cls(_Columns(times, {"open": open, "high": high, "low": low, "close": close}))

    def __len__(self) -> int:
        return len(self.close)

    def structure(self, length: int) -> Structure:
        if length not in self._structures:
            self._structures[length] = analyze_structure(self.high, self.low, self.close, length)
        return self._structures[length]

    @cached_property
    def atr(self) -> np.ndarray:
        """ATR(14): the mean of the last 14 true ranges (the first bars average what there is)."""
        return average_true_range(self.high, self.low, self.close, ATR_LENGTH)


@dataclass(frozen=True)
class _Columns:
    index: Sequence[datetime]
    columns: dict

    def __getitem__(self, name: str):
        return self.columns[name]


def average_true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int) -> np.ndarray:
    """Simple mean of the true range over ``length`` bars, ending at each bar (the first bars
    average what there is). The true range of the first bar is its high minus its low."""
    n = len(close)
    if n == 0:
        return np.zeros(0)
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    padded = np.concatenate([np.zeros(length - 1), tr])  # zeros add nothing to the first windows' sums
    sums = np.lib.stride_tricks.sliding_window_view(padded, length).sum(axis=1)
    return sums / np.minimum(np.arange(1, n + 1), length)


@dataclass(frozen=True)
class Bias:
    """Direction of the latest break of structure on a timeframe."""

    direction: Literal["bullish", "bearish"]
    event: Literal["BOS", "CHoCH"]  # CHoCH = the break flipped the previous direction
    level: float  # price of the swing that was broken
    swing_time: datetime  # when that swing printed
    break_time: datetime  # candle that closed beyond it
    bars_ago: int  # closed candles since the break candle
    streak: int  # consecutive breaks in this direction, including this one

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "event": self.event,
            "level": self.level,
            "swing_time": self.swing_time.isoformat(),
            "swing_time_unix": unix(self.swing_time),
            "break_time": self.break_time.isoformat(),
            "break_time_unix": unix(self.break_time),
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
