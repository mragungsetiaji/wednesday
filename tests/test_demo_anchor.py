"""Demo data from today's real price: the random walk passes through the newest stored real bar."""

import pandas as pd

from wednesday.engine import Runtime, real_price
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.settings import DataSettings
from wednesday.storage import Store

END = pd.Timestamp("2026-10-03 12:00")


def bars(price, last, n=5):
    idx = pd.date_range(end=last, periods=n, freq="1min")
    return pd.DataFrame({"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1.0}, index=idx)


def test_walk_passes_through_the_real_price_then_walks_on():
    at = pd.Timestamp("2026-10-02 20:59")
    feed = SyntheticFeed(history=3000, end=END, anchor=(at, 4172.1))
    b = feed._bars
    assert b.loc[at, "close"] == 4172.1
    assert b.index[-1] == END - pd.Timedelta(minutes=1) and len(b) == 3000
    assert b.loc[at + pd.Timedelta(minutes=1), "open"] == 4172.1  # the walk carries on from there
    assert (b["high"] >= b[["open", "close"]].max(axis=1)).all() and (b["low"] <= b[["open", "close"]].min(axis=1)).all()
    assert abs(b["close"].iloc[0] - 4172.1) < 500  # nowhere near the old 2650 start


def test_anchor_after_the_end_or_before_the_start():
    later = SyntheticFeed(history=500, end=END, anchor=(END + pd.Timedelta(days=1), 4000.0))
    assert later._bars["close"].iloc[-1] == 4000.0
    earlier = SyntheticFeed(history=500, end=END, anchor=(END - pd.Timedelta(days=30), 4000.0))
    assert earlier._bars["open"].iloc[0] == 4000.0


def test_real_price_prefers_mt5_ignores_the_sample_and_converts_the_clock(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'd.db'}")
    assert real_price(store) is None
    store.save_bars("sample", "XAUUSD", bars(2500.0, "2026-10-03 11:00"))
    assert real_price(store) is None  # the journal's made-up bars aren't market data
    store.save_bars("yfinance", "GC=F", bars(4180.0, "2026-10-02 20:59"))
    assert real_price(store) == (pd.Timestamp("2026-10-02 20:59"), 4180.0)
    store.save_bars("mt5", "XAUUSD", bars(4170.0, "2026-10-02 23:58"))  # broker clock, NY+7 = 20:58 UTC
    assert real_price(store) == (pd.Timestamp("2026-10-02 20:58"), 4170.0)


def test_demo_runtime_starts_from_the_stored_price(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'd.db'}")
    last = (pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(hours=2)).floor("min")
    store.save_bars("yfinance", "GC=F", bars(4172.1, last))
    runtime = Runtime(ScanConfig(lookback=20), DataSettings(source="synthetic"), store)
    assert runtime.engine.feed._bars.loc[last, "close"] == 4172.1
