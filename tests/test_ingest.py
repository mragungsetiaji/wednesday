"""The ingest of the source's own timeframes (ingest.py), with a stand-in feed."""

from __future__ import annotations

import pandas as pd

from wednesday.ingest import NATIVE, Ingest, closed, forming, merge
from wednesday.storage import Store
from wednesday.timeframes import OHLCV_COLUMNS


def frame(times, closes):
    idx = pd.DatetimeIndex(pd.to_datetime(times))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0})[OHLCV_COLUMNS]


def test_only_closed_candles_are_kept():
    bars = frame(["2026-11-24 03:00", "2026-11-24 07:00", "2026-11-24 11:00"], [1, 2, 3])
    assert len(closed(bars, 240, pd.Timestamp("2026-11-24 12:00"))) == 2  # 11:00 forms until 15:00
    assert len(closed(bars, 240, pd.Timestamp("2026-11-24 15:00"))) == 3


def test_merge_keeps_the_newer_bar_and_the_last_few():
    out = merge(frame(["2026-11-24 03:00", "2026-11-24 07:00"], [1, 2]),
                frame(["2026-11-24 07:00", "2026-11-24 11:00"], [9, 3]), keep=2)
    assert out["close"].tolist() == [9, 3]


def test_the_forming_candle_follows_the_native_grid():
    native = frame(["2026-11-24 03:00", "2026-11-24 07:00"], [1, 2])
    m1 = frame(["2026-11-24 10:59", "2026-11-24 11:00", "2026-11-24 11:01", "2026-11-24 15:02"], [5, 6, 4, 8])
    out = forming(native, m1, 240)
    assert list(out.index) == [pd.Timestamp("2026-11-24 11:00"), pd.Timestamp("2026-11-24 15:00")]
    assert out.iloc[0][["open", "close", "high", "low"]].tolist() == [6, 4, 7, 3]


class NativeFeed:
    """Each timeframe's candles up to an hour ago, a fixed history of 30 candles."""

    name = "tradingview"

    def __init__(self):
        self.calls = []

    def fetch_native(self, resolution, count, to=None):
        self.calls.append((resolution, count, to))
        n = next(n for n in NATIVE if n.resolution == resolution)
        step = pd.Timedelta(minutes=n.minutes)
        end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor(step)
        times = [end - i * step for i in range(30)][::-1]
        bars = frame(times, range(30))
        if to is not None:
            bars = bars[bars.index < pd.Timestamp(to, unit="s")]
        return bars.tail(count)


def test_refresh_stores_each_timeframe_and_backfill_ends(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    feed = NativeFeed()
    ingest = Ingest("tradingview", "FX:XAUUSD", store)
    assert ingest.refresh(feed)
    assert set(ingest.native) == {n.scan for n in NATIVE}
    assert len(ingest.native["4H"]) == 29  # the forming one is left out
    n, _, _ = store.bar_bounds("tradingview", "FX:XAUUSD", "4h")
    assert n == 29
    assert not ingest.refresh(feed)  # nothing new closed yet: no fetch at all
    assert len(feed.calls) == len(NATIVE)
    # The stand-in has no older history: each series' backfill finds nothing and ends.
    while ingest.backfill(feed):
        pass
    rows = {r["tf"]: r for r in store.series("tradingview", "FX:XAUUSD")}
    assert {tf: r["backfill"] for tf, r in rows.items() if tf != "1m"} == {n.tf: "done" for n in NATIVE}
    assert all(r["active"] for r in rows.values())
    ingest.stopped()
    assert not any(r["active"] for r in store.series("tradingview", "FX:XAUUSD"))


def test_a_source_without_its_own_timeframes_is_left_alone():
    class Synthetic:
        name = "synthetic"

        def fetch_native(self, resolution, count, to=None):
            return None

    ingest = Ingest("synthetic", "XAUUSD")
    assert not ingest.refresh(Synthetic())
    assert ingest.native == {}
