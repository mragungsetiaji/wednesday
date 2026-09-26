"""Older candles for the chart as it scrolls left: engine memory, then MT5's own candles or stored M1."""

import sys
import time
import types

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.feeds import MT5Feed, SyntheticFeed
from wednesday.history import older_candles
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME, resample_ohlcv

from test_mt5_and_cli import FakeMT5

H1 = TIMEFRAMES_BY_NAME["1H"]
M1_BARS = SyntheticFeed(seed=3, history=60 * 24 * 10, end=pd.Timestamp("2026-03-02 12:00")).fetch_m1(60 * 24 * 10)


def fake_engine(feed, m1=None, store=None, key=None):
    return types.SimpleNamespace(
        feed=feed,
        snapshot=lambda: (1, None, m1),
        call=lambda fn: fn(feed),
        buffer=types.SimpleNamespace(store=store, key=key),
    )


def unix(ts):
    return int(ts.timestamp())


def pages(engine, tf, before, limit):
    """Scroll back page by page until there is nothing older."""
    out = []
    while True:
        df, more = older_candles(engine, tf, before, limit)
        out.append(df)
        if not more or df.empty:
            return pd.concat(out[::-1])
        before = unix(df.index[0])


def test_memory_then_stored_bars_page_back_without_gaps(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'h.db'}")
    store.save_bars("yfinance", "XAUUSD", M1_BARS)
    memory = M1_BARS.tail(60 * 30)  # the engine keeps 30 hours
    engine = fake_engine(SyntheticFeed(), m1=memory, store=store, key=("yfinance", "XAUUSD"))
    latest = resample_ohlcv(memory, H1).tail(10)

    got = pages(engine, H1, unix(latest.index[0]), limit=25)
    expected = resample_ohlcv(M1_BARS, H1)
    expected = expected[expected.index < latest.index[0]]
    assert got.index.is_unique and got.index.is_monotonic_increasing
    # Every whole hour is back, with the same prices as resampling everything at once.
    pd.testing.assert_frame_equal(got.iloc[1:], expected.iloc[1:], check_freq=False)


def test_memory_only_feed_stops():
    engine = fake_engine(SyntheticFeed(), m1=M1_BARS.tail(600))
    df, more = older_candles(engine, H1, unix(M1_BARS.index[-1]), limit=100)
    assert 0 < len(df) < 100 and more is False


class HistoryMT5(FakeMT5):
    TIMEFRAME_H1 = 16385

    def __init__(self, end, bars):
        super().__init__()
        self.end, self.bars, self.calls = end, bars, []

    def copy_rates_from(self, symbol, timeframe, date_from, count):
        self.calls.append((timeframe, date_from, count))
        assert timeframe == self.TIMEFRAME_H1 and isinstance(date_from, int)
        times = np.arange(self.end - 3600 * self.bars, self.end, 3600)
        times = times[times <= date_from][-count:]
        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                 ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")]
        rates = np.zeros(len(times), dtype=dtype)
        rates["time"] = times
        rates["open"] = rates["high"] = rates["low"] = rates["close"] = 2650.0
        return rates


def test_mt5_serves_the_timeframe_itself(monkeypatch):
    end = unix(pd.Timestamp("2026-03-02 00:00"))
    mt5 = HistoryMT5(end, bars=120)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    feed = MT5Feed("XAUUSD")
    feed.connect()
    engine = fake_engine(feed)  # nothing in memory: straight to the terminal

    df, more = older_candles(engine, H1, end, limit=50)
    assert len(df) == 50 and more is True
    assert unix(df.index[-1]) == end - 3600  # strictly before
    got = pages(engine, H1, end, limit=50)
    assert len(got) == 120 and got.index.is_unique
    assert all(tf == mt5.TIMEFRAME_H1 for tf, _, _ in mt5.calls)  # never M1


def test_candles_endpoint_pages_back(tmp_path):
    from wednesday.detectors import DetectorParams
    from wednesday.engine import Runtime
    from wednesday.scanner import ScanConfig
    from wednesday.server import create_app
    from wednesday.settings import DataSettings

    runtime = Runtime(ScanConfig(lookback=30, params=DetectorParams(swing_length=2)), DataSettings(source="synthetic"), None)
    runtime.start()
    try:
        api = TestClient(create_app(runtime, ui_dir=tmp_path))
        deadline = time.monotonic() + 10
        while runtime.engine.state.version == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        latest = api.get("/api/candles", params={"tf": "5M", "limit": 20}).json()["candles"]
        body = api.get("/api/candles", params={"tf": "5M", "limit": 20, "before": latest[0]["time"]}).json()
        assert len(body["candles"]) == 20 and body["candles"][-1]["time"] == latest[0]["time"] - 300
        before, pages_seen = body["candles"][0]["time"], 1
        while body["has_more"] and pages_seen < 200:
            body = api.get("/api/candles", params={"tf": "5M", "limit": 200, "before": before}).json()
            before, pages_seen = (body["candles"][0]["time"] if body["candles"] else before), pages_seen + 1
        assert body["has_more"] is False  # demo data: memory only, then it stops
        assert api.get("/api/candles", params={"tf": "9M", "before": 1}).status_code == 404
    finally:
        runtime.stop()
