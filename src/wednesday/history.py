"""Candles older than the chart's window, loaded as the chart scrolls left.

The scan only needs ``lookback`` candles per timeframe, so the engine keeps just
enough M1 bars for that. Older candles come from, in order: the engine's M1
buffer, the feed's own candles of that timeframe (MT5: no M1 history needed),
or the M1 bars stored in the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from .timeframes import OHLCV_COLUMNS, Timeframe, resample_ohlcv

if TYPE_CHECKING:
    from .engine import Engine


def older_candles(engine: Engine, tf: Timeframe, before: int, limit: int) -> tuple[pd.DataFrame, bool]:
    """Up to ``limit`` candles opening before ``before`` (unix, feed clock), and whether older ones may exist."""
    edge = pd.Timestamp(before, unit="s")
    parts: list[pd.DataFrame] = []
    _, _, m1 = engine.snapshot()
    if m1 is not None and len(m1):
        # The first candle may open before the buffer's first bar: leave it to the older sources.
        mem = resample_ohlcv(m1, tf, drop_incomplete=False).iloc[1:]
        mem = mem[mem.index < edge].tail(limit)
        if len(mem):
            parts.append(mem)
            edge = mem.index[0]
    rest = limit - sum(len(p) for p in parts)
    more = True
    if rest > 0:
        older, more = _beyond_memory(engine, tf, edge, rest)
        if len(older):
            parts.insert(0, older)
    return (pd.concat(parts) if parts else _empty()), more


def _beyond_memory(engine: Engine, tf: Timeframe, edge: pd.Timestamp, count: int) -> tuple[pd.DataFrame, bool]:
    if engine.feed.native_history:
        # MT5 is only used from the scan thread.
        df = engine.call(lambda feed: feed.fetch_history(tf, edge, count))
        return df.tail(count), len(df) >= count
    store, key = engine.buffer.store, engine.buffer.key
    if store is None or key is None:
        return _empty(), False
    wanted = count * tf.minutes
    m1 = store.load_bars(*key, wanted, before=int(edge.timestamp()))
    if m1.empty:
        return _empty(), False
    candles = resample_ohlcv(m1, tf, drop_incomplete=False)
    full = len(m1) >= wanted
    if full and len(candles) > 1:
        candles = candles.iloc[1:]  # may be partial; the next page loads it whole
    return candles[candles.index < edge].tail(count), full


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([]), dtype=float)
