"""Live price between the minute scans: settings, the forming candle, and /api/tick."""

import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings, resolve
from wednesday.timeframes import TIMEFRAMES_BY_NAME

END = pd.Timestamp("2026-03-02 12:00")


@pytest.fixture
def engine():
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=END)
    return Engine(feed, cfg, "XAUUSD", tick_seconds=2)


class FixedTicks:
    """Hands out the given (time, price) ticks instead of the synthetic feed's own."""

    def __init__(self, ticks):
        self.ticks = iter(ticks)

    def __call__(self):
        return next(self.ticks)


def test_tick_defaults_and_validation():
    assert DataSettings(source="yfinance").resolved_tick == 60
    assert DataSettings(source="mt5").resolved_tick == 0.25
    assert DataSettings(source="csv").resolved_tick == 0
    assert DataSettings(source="mt5", tick_seconds=5).resolved_tick == 5
    assert DataSettings(source="mt5", tick_seconds=0).validate() == []
    assert DataSettings(source="yfinance", tick_seconds=15).validate() == []
    assert DataSettings(source="yfinance", tick_seconds=5).validate()  # under Yahoo's minimum
    assert DataSettings(source="mt5", tick_seconds=120).validate()
    assert DataSettings(source="csv", tick_seconds=1).validate()  # a CSV has no live price
    assert DataSettings.from_dict({"source": "mt5", "tick_seconds": "0.5"}).tick_seconds == 0.5


def test_switching_source_resets_the_tick_interval():
    class Store:
        def get_setting(self, key):
            return {"source": "mt5", "tick_seconds": 0.5}

    assert resolve({}, Store()).resolved_tick == 0.5
    assert resolve({"source": "yfinance"}, Store()).resolved_tick == 60


def test_ticks_build_the_forming_candle(engine, monkeypatch):
    engine.step()
    last = engine.state.m1.index[-1]
    minute = last + pd.Timedelta(minutes=1)
    monkeypatch.setattr(engine.feed, "last_tick", FixedTicks([
        (minute + pd.Timedelta(seconds=5), 2000.0),
        (minute + pd.Timedelta(seconds=20), 2003.5),
        (minute + pd.Timedelta(seconds=40), 1998.0),
        (minute + pd.Timedelta(seconds=55), 2001.0),
    ]))
    for _ in range(4):
        engine.tick()
    live = engine.state.live
    assert live["price"] == 2001.0 and engine.state.tick == 4
    assert live["bar"] == {"time": minute, "open": 2000.0, "high": 2003.5, "low": 1998.0, "close": 2001.0}


def test_stale_tick_moves_the_price_but_starts_no_candle(engine, monkeypatch):
    engine.step()
    long_after = engine.state.m1.index[-1] + pd.Timedelta(hours=2)  # e.g. the market is closed
    monkeypatch.setattr(engine.feed, "last_tick", lambda: (long_after, 1234.5))
    engine.tick()
    assert engine.state.live["price"] == 1234.5 and engine.state.live["bar"] is None


def test_tick_errors_are_swallowed(engine, monkeypatch):
    engine.step()

    def boom():
        raise ConnectionError("offline")

    monkeypatch.setattr(engine.feed, "last_tick", boom)
    engine.tick()
    assert engine.state.live is None and engine.state.error is None


def test_synthetic_ticks_become_the_closed_bar(engine):
    engine.step()
    open_ = engine.state.m1["close"].iloc[-1]
    prices = [engine.feed.last_tick()[1] for _ in range(5)]
    engine.step()
    bar = engine.state.m1.iloc[-1]
    assert bar["open"] == open_ and bar["close"] == prices[-1]
    assert bar["high"] == max(prices + [open_]) and bar["low"] == min(prices + [open_])


def test_scan_drops_the_forming_candle_once_its_minute_closes(engine):
    engine.step()
    engine.tick()
    minute = engine.state.live["bar"]["time"]
    engine.step()  # the synthetic feed closes that minute
    assert engine.state.m1.index[-1] == minute
    assert engine.state.live["bar"] is None


def test_tick_endpoint(engine, tmp_path):
    client = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))
    assert client.get("/api/tick").json() == {"version": 0, "tick": 0, "price": None, "time": None, "bar": None}
    engine.step()
    engine.tick()
    body = client.get("/api/tick").json()
    assert body["version"] == 1 and body["tick"] == 1
    assert body["bar"]["close"] == body["price"]
    assert body["bar"]["time"] == int(engine.state.live["bar"]["time"].timestamp())
    assert client.get("/api/status").json()["tick_seconds"] == 2


def test_no_poll_scans_once():
    """--no-poll: one scan at start, then no more scans and no live prices."""
    cfg = ScanConfig(lookback=40, timeframes=(TIMEFRAMES_BY_NAME["5M"],))
    engine = Engine(SyntheticFeed(), cfg, "XAUUSD", tick_seconds=2, poll=False)
    assert engine.tick_seconds == 0
    scans = []
    engine.start(on_result=scans.append)
    deadline = time.monotonic() + 10
    while not scans and time.monotonic() < deadline:
        time.sleep(0.05)
    assert engine.call(lambda feed: feed.name) == "synthetic"  # queued feed work still runs
    engine.stop()
    assert not engine._thread.is_alive()
    assert len(scans) == 1
