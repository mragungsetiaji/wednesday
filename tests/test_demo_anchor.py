"""Demo data from today's real price: the random walk passes through the newest stored real bar."""

import pandas as pd

from wednesday.engine import Runtime, real_history, real_price
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


def test_demo_shows_the_real_candles_then_walks_on():
    real = bars(4172.1, "2026-10-02 20:59", n=600)
    real["high"] = real["close"] + 2
    feed = SyntheticFeed(history=3000, end=END, real=real)
    b = feed._bars
    assert len(b) == 3000 and b.index.is_monotonic_increasing and b.index[-1] == END - pd.Timedelta(minutes=1)
    pd.testing.assert_frame_equal(b.loc[real.index], real[b.columns], check_freq=False)  # the real candles, untouched
    first = real.index[0]
    assert b["close"][b.index < first].iloc[-1] == real["open"].iloc[0]  # the lead-in joins the first real open
    after = b[b.index > real.index[-1]]
    assert len(after) and after["open"].iloc[0] == 4172.1  # and the walk carries on from the last real close


def test_real_history_in_utc(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'd.db'}")
    store.save_bars("mt5", "XAUUSD", bars(4170.0, "2026-10-02 23:58", n=3))  # broker clock NY+7
    h = real_history(store)
    assert list(h.index) == list(pd.date_range(end="2026-10-02 20:58", periods=3, freq="1min"))


def test_demo_runtime_starts_from_the_stored_price(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'd.db'}")
    last = (pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(hours=2)).floor("min")
    store.save_bars("yfinance", "GC=F", bars(4172.1, last))
    runtime = Runtime(ScanConfig(lookback=20), DataSettings(source="synthetic"), store)
    b = runtime.engine.feed._bars
    assert b.loc[last, "close"] == 4172.1 and b.loc[last - pd.Timedelta(minutes=1), "close"] == 4172.1  # real bars
