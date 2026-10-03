"""Bar replay (#23): the market as it stood at a past minute, for practice and honest labels.

A replay clock ``at`` (unix seconds, feed clock) cuts the M1 history: only bars that had
closed by then (open time + 1 minute <= at) are used. The scan runs on that cut exactly as
it ran live, and the chart's candles are built from it, including the candle still forming
at ``at`` from the minutes it had so far. Nothing after the clock is ever returned.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

import pandas as pd

from .scanner import ScanConfig, scan
from .timeframes import M1, Timeframe, resample_ohlcv

CACHE_SIZE = 64  # replay views kept, so stepping back and forth doesn't rescan


def cut(m1: pd.DataFrame, at: int) -> pd.DataFrame:
    """The M1 bars closed by ``at``: open time + 1 minute <= at."""
    return m1[m1.index + M1 <= pd.Timestamp(int(at), unit="s")]


def view(m1: pd.DataFrame, cfg: ScanConfig, at: int, tf: Timeframe, limit: int) -> dict:
    """The scan and ``tf`` candles at the replay clock ``at`` (``m1``: any history covering it)."""
    bars = cut(m1, at).tail(cfg.required_m1_bars())
    if bars.empty:
        raise ValueError("No stored bars before that time: pick a later start")
    result = scan(bars, cfg)
    candles = resample_ohlcv(bars, tf, drop_incomplete=False).tail(limit)
    tf_result = next((r for r in result.results if r.timeframe.name == tf.name), None)
    return {
        "at": int(at),
        "bar_time": int(bars.index[-1].timestamp()),  # the last closed minute
        "timeframe": tf.name,
        "price": result.price,
        "candles": candles,
        "levels": [lv.to_dict() for s in tf_result.sets.values() for lv in s.active] if tf_result else [],
        "swings": [sw.to_dict() for sw in tf_result.swings] if tf_result else [],
        "scan": result.to_dict(None),  # today's bias isn't applied to the past
    }


class ReplayCache:
    """The last few replay views by (stream, at, timeframe, limit)."""

    def __init__(self, size: int = CACHE_SIZE):
        self.size = size
        self._items: OrderedDict = OrderedDict()
        self._lock = Lock()

    def get(self, key, make):
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        value = make()
        with self._lock:
            self._items[key] = value
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return value
