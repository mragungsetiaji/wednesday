"""The feed thread keeps ticking while the scan worker scans (#25)."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from wednesday import engine as engine_mod
from wednesday.detectors import DetectorParams
from wednesday.engine import Engine
from wednesday.feeds import MT5Feed, SyntheticFeed
from wednesday.scanner import ScanConfig

CFG = ScanConfig(lookback=60, params=DetectorParams(swing_length=3))


def make_engine(**kw):
    feed = SyntheticFeed(seed=3, history=CFG.required_m1_bars() + 50)
    return Engine(feed, CFG, "XAUUSD", tick_seconds=0.05, **kw)


def test_a_slow_scan_doesnt_stop_the_live_price(monkeypatch):
    real = engine_mod.scan
    scanning = threading.Event()

    def slow_scan(*a, **k):
        scanning.set()
        time.sleep(1.0)
        return real(*a, **k)

    monkeypatch.setattr(engine_mod, "scan", slow_scan)
    eng = make_engine()
    eng.start()
    assert scanning.wait(10)
    before = eng.state.tick
    time.sleep(0.6)  # still inside the slow scan
    during = eng.state.tick
    assert eng.state.version == 0  # the scan hasn't finished
    assert during - before >= 5  # ticks kept coming every 0.05 s
    eng.stop()


def test_on_result_runs_off_the_feed_thread():
    eng = make_engine()
    seen = {}
    done = threading.Event()

    def on_result(result):
        seen["thread"] = threading.current_thread().name
        seen["feed"] = eng.call(lambda feed: threading.current_thread().name)  # feed work still reachable
        done.set()

    eng.start(on_result=on_result)
    assert done.wait(10)
    eng.stop()
    assert seen == {"thread": "scan-worker", "feed": "scan-engine"}


def test_call_gives_up_at_once_when_stopping(monkeypatch):
    """The feed thread, closing, waits for the scan worker; a worker waiting on call() must not hang it."""
    eng = make_engine()
    eng._thread = threading.current_thread()  # looks alive, but nothing will run the task
    monkeypatch.setattr(eng._tasks, "put", lambda task: None)
    eng._stop.set()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="stopping"):
        eng.call(lambda f: None, timeout=30)
    assert time.monotonic() - started < 1


class TickMT5:
    """copy_ticks_from and symbol_info_tick over a fixed list of ticks."""

    COPY_TICKS_INFO = 2

    def __init__(self, ticks):
        dt = np.dtype([("time_msc", "i8"), ("bid", "f8")])
        self.ticks = np.array(ticks, dtype=dt)

    def symbol_info_tick(self, symbol):
        t = self.ticks[0]
        return SimpleNamespace(time=int(t["time_msc"]) // 1000, time_msc=int(t["time_msc"]), bid=float(t["bid"]))

    def copy_ticks_from(self, symbol, start, count, flags):
        return self.ticks[self.ticks["time_msc"] >= start * 1000][:count]


def test_mt5_reads_every_tick_since_the_last_poll():
    base = int(pd.Timestamp("2026-03-02 12:00:00").value // 1_000_000)
    feed = MT5Feed("XAUUSD")
    feed._mt5 = TickMT5([(base, 2400.0), (base + 100, 2405.0), (base + 200, 2398.0), (base + 300, 2401.0)])
    first = feed.new_ticks()
    assert [p for _, p in first] == [2400.0]  # the first poll takes the latest tick to start from
    rest = feed.new_ticks()
    assert [p for _, p in rest] == [2405.0, 2398.0, 2401.0]  # the high and low between polls aren't lost
    assert feed.new_ticks() == []  # nothing new


def test_folding_several_ticks_keeps_the_high_and_low():
    eng = make_engine()
    eng.step()
    last = eng.state.m1.index[-1]
    t0 = last + pd.Timedelta(minutes=1, seconds=5)
    eng.feed.new_ticks = lambda: [(t0, 2400.0), (t0 + pd.Timedelta(seconds=1), 2410.0),
                                  (t0 + pd.Timedelta(seconds=2), 2390.0), (t0 + pd.Timedelta(seconds=3), 2401.0)]
    eng.tick()
    bar = eng.state.live["bar"]
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (2400.0, 2410.0, 2390.0, 2401.0)
