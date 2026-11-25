import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.engine import Engine, Runtime
from wednesday.feeds import DataFeed, M1Buffer
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import SETTINGS_KEY, DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME


def bars(start, n, base=2650.0):
    idx = pd.date_range(start, periods=n, freq="1min")
    close = base + pd.Series(range(n), dtype=float).to_numpy() * 0.1
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0}, index=idx)


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'data' / 'xau.db'}")


def test_store_bars_roundtrip_and_upsert(store):
    df = bars("2026-03-02 10:00", 5)
    assert store.save_bars("yfinance", "GC=F", df) == 5
    changed = df.iloc[-2:].copy()
    changed["close"] = 1.0
    store.save_bars("yfinance", "GC=F", pd.concat([changed, bars("2026-03-02 10:05", 2)]))
    out = store.load_bars("yfinance", "GC=F", limit=100)
    assert len(out) == 7
    assert out.index[0] == pd.Timestamp("2026-03-02 10:00")
    assert out["close"].iloc[4] == 1.0  # updated, not duplicated
    assert len(store.load_bars("yfinance", "GC=F", limit=3)) == 3
    assert store.load_bars("mt5", "XAUUSD", limit=10).empty
    (stat,) = store.bar_stats()
    assert (stat["source"], stat["symbol"], stat["bars"]) == ("yfinance", "GC=F", 7)
    assert stat["last"] == "2026-03-02T10:06:00"


def test_store_keeps_each_timeframe_apart(store):
    store.save_bars("tradingview", "FX:XAUUSD", bars("2026-03-02 10:00", 3))
    four = bars("2026-03-02 03:00", 2).set_axis(pd.DatetimeIndex(["2026-03-02 03:00", "2026-03-02 07:00"]))
    store.save_bars("tradingview", "FX:XAUUSD", four, tf="4h")
    assert len(store.load_bars("tradingview", "FX:XAUUSD", 100)) == 3
    assert list(store.load_bars("tradingview", "FX:XAUUSD", 100, tf="4h").index.hour) == [3, 7]
    assert store.bar_bounds("tradingview", "FX:XAUUSD", "4h")[0] == 2
    assert [s["bars"] for s in store.bar_stats()] == [3]  # the M1 streams
    assert store.delete_bars("tradingview") == 5


def test_an_old_m1_bars_table_moves_to_bars(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE m1_bars (source VARCHAR(32), symbol VARCHAR(64), time BIGINT, open FLOAT, "
                   "high FLOAT, low FLOAT, close FLOAT, volume FLOAT, PRIMARY KEY (source, symbol, time))")
        db.execute("INSERT INTO m1_bars VALUES ('tradingview', 'FX:XAUUSD', 1772445600, 1, 2, 0.5, 1.5, 3)")
    s = Store(f"sqlite:///{path}")
    out = s.load_bars("tradingview", "FX:XAUUSD", 10)
    assert list(out["close"]) == [1.5] and out.index[0] == pd.Timestamp(1772445600, unit="s")
    with sqlite3.connect(path) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "m1_bars" not in names
        assert db.execute("SELECT version, name FROM schema_migrations").fetchall() == [(1, "bars_from_m1_bars")]
    Store(f"sqlite:///{path}")  # opening again runs nothing twice


def test_store_settings_and_describe(store):
    assert store.get_setting(SETTINGS_KEY) is None
    store.set_setting(SETTINGS_KEY, {"source": "mt5"})
    store.set_setting(SETTINGS_KEY, {"source": "yfinance"})
    assert store.get_setting(SETTINGS_KEY) == {"source": "yfinance"}
    store.delete_setting(SETTINGS_KEY)
    assert store.get_setting(SETTINGS_KEY) is None
    assert store.describe()["backend"] == "sqlite"
    url = Store("sqlite://").url.set(drivername="postgresql", username="u", password="secret", host="h", database="d")
    assert "secret" not in url.render_as_string(hide_password=True)


class ListFeed(DataFeed):
    """Serves a fixed M1 frame; `until` moves the 'now' forward."""

    name = "listfeed"

    def __init__(self, df):
        self.df = df
        self.until = df.index[-1]
        self.calls = []

    def fetch_m1(self, count):
        self.calls.append(count)
        return self.df[self.df.index <= self.until].tail(count)


def test_buffer_persists_and_resumes_from_store(store):
    all_bars = bars("2026-03-02 00:00", 200)
    feed = ListFeed(all_bars)
    feed.until = all_bars.index[99]
    buf = M1Buffer(feed, max_bars=150, update_bars=10, store=store, key=("listfeed", "XAU"))
    assert len(buf.update()) == 100
    assert feed.calls == [150]
    assert len(store.load_bars("listfeed", "XAU", 1000)) == 100

    # A new process: history comes from the store, only a small update is fetched.
    feed2 = ListFeed(all_bars)
    feed2.until = all_bars.index[104]
    buf2 = M1Buffer(feed2, max_bars=150, update_bars=10, store=store, key=("listfeed", "XAU"))
    m1 = buf2.update()
    assert feed2.calls == [10]
    assert len(m1) == 105 and m1.index[-1] == all_bars.index[104]
    assert len(store.load_bars("listfeed", "XAU", 1000)) == 105

    # Feed with a short window (like Yahoo's 7 days): stored history is kept beyond it.
    short = ListFeed(all_bars.iloc[150:])  # bars 150..199 only
    buf3 = M1Buffer(short, max_bars=500, update_bars=10, store=store, key=("listfeed", "XAU"))
    m1 = buf3.update()
    assert short.calls == [10, 500]  # gap after bar 104 -> full fetch
    assert m1.index[0] == all_bars.index[0] and m1.index[-1] == all_bars.index[199]
    assert len(m1) == 155  # 0..104 from the store + 150..199 from the feed


def test_synthetic_feed_is_not_stored(store):
    from wednesday.feeds import SyntheticFeed

    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["5M"],))
    engine = Engine(SyntheticFeed(history=500), cfg, "XAUUSD", store)
    engine.step()
    assert store.bar_stats() == []


class FailingConnectFeed(ListFeed):
    name = "failing"

    def __init__(self, df, failures):
        super().__init__(df)
        self.failures = failures

    def connect(self):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("terminal not running")


def test_engine_retries_connect_and_reports_error(monkeypatch):
    import wednesday.engine as engine_mod

    monkeypatch.setattr(engine_mod, "seconds_to_next_minute", lambda delay: 0.01)
    cfg = ScanConfig(lookback=5, timeframes=(TIMEFRAMES_BY_NAME["5M"],))
    engine = Engine(FailingConnectFeed(bars("2026-03-02 00:00", 100), failures=2), cfg, "XAU")
    seen_error = []
    engine.start(0)
    deadline = time.time() + 5
    while time.time() < deadline and engine.state.version == 0:
        if engine.state.error:
            seen_error.append(engine.state.error)
        time.sleep(0.002)
    engine.stop()
    assert any("terminal not running" in e for e in seen_error)
    assert engine.state.version >= 1 and engine.state.error is None


def test_settings_api_switches_source_live(store, tmp_path):
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["5M"],), params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, delay=0)
    first_engine = runtime.engine
    client = TestClient(create_app(runtime, ui_dir=tmp_path))

    body = client.get("/api/settings").json()
    assert body["editable"] is True
    assert body["settings"]["source"] == "synthetic"
    assert [s["id"] for s in body["sources"]] == ["tradingview"]  # synthetic is for development only
    assert body["storage"]["backend"] == "sqlite"
    assert body["running"]["bars_needed"] == cfg.required_m1_bars()

    for gone in ("nope", "yfinance", "csv"):
        assert client.put("/api/settings", json={"source": gone}).status_code == 422

    res = client.put("/api/settings", json={"source": "synthetic", "symbol": "XAUTEST", "tick_seconds": 0})
    assert res.status_code == 200, res.text
    assert runtime.engine is not first_engine
    assert store.get_setting(SETTINGS_KEY)["symbol"] == "XAUTEST"
    deadline = time.time() + 5
    while time.time() < deadline and runtime.engine.state.version == 0:
        time.sleep(0.02)
    assert client.get("/api/status").json()["source"] == "synthetic"
    assert client.get("/api/scan").json()["scan"] is not None
    assert client.get("/api/settings").json()["storage"]["series"] == []  # synthetic bars are never stored
    runtime.stop()


def test_settings_api_read_only_for_fixed_engine(tmp_path):
    from wednesday.feeds import SyntheticFeed

    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["5M"],))
    client = TestClient(create_app(Engine(SyntheticFeed(history=500), cfg, "XAUUSD"), source="synthetic", ui_dir=tmp_path))
    body = client.get("/api/settings").json()
    assert body["editable"] is False and body["storage"] is None
    assert client.put("/api/settings", json={"source": "synthetic"}).status_code == 409
