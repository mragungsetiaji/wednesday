"""Feed connection states, MT5 giving up instead of reopening the terminal, and the MT5 password."""

import sys
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import wednesday
from wednesday import engine as engine_mod
from wednesday import mt5_terminals
from wednesday import secret_store
from wednesday.detectors import DetectorParams
from wednesday.engine import RECONNECT_ATTEMPTS, Engine, Runtime
from wednesday.feeds import MT5Feed, SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings

from test_mt5_and_cli import FakeMT5

CFG = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))


class FlakyFeed(SyntheticFeed):
    """Synthetic bars, with connect() and fetch_m1() failing on demand."""

    def __init__(self, retry_connect=True, connect_fails=0, fetch_fails_after=None):
        super().__init__(seed=7, history=CFG.required_m1_bars() + 100, end=pd.Timestamp("2026-03-02 12:00"))
        self.retry_connect = retry_connect
        self.connect_fails = connect_fails
        self.fetch_fails_after = fetch_fails_after
        self.connects = self.fetches = 0

    def connect(self):
        self.connects += 1
        if self.connects <= self.connect_fails:
            raise RuntimeError("terminal not logged in")

    def fetch_m1(self, count):
        self.fetches += 1
        if self.fetch_fails_after is not None and self.fetches > self.fetch_fails_after:
            raise RuntimeError("connection lost")
        return super().fetch_m1(count)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(engine_mod, "seconds_to_next_minute", lambda delay: 0.01)


def run_until(engine: Engine, done, timeout=5.0):
    engine.start()
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    engine.stop()


def test_failed_first_connect_gives_up_without_retrying():
    feed = FlakyFeed(retry_connect=False, connect_fails=99)
    engine = Engine(feed, CFG, "XAUUSD")
    engine.start()
    engine._thread.join(5)
    assert not engine._thread.is_alive()  # stopped by itself
    assert feed.connects == 1  # connecting starts the MT5 terminal: once only
    assert engine.state.conn == "failed"
    assert "not logged in" in engine.state.error
    with pytest.raises(RuntimeError, match="Reconnect"):
        engine.call(lambda f: None)


def test_lost_connection_reconnects_then_gives_up():
    feed = FlakyFeed(retry_connect=False, fetch_fails_after=1)
    engine = Engine(feed, CFG, "XAUUSD")
    seen = set()

    def done():
        seen.add((engine.state.conn, engine.state.attempt))
        return engine.state.conn == "failed"

    run_until(engine, done)
    assert engine.state.conn == "failed"
    assert engine.state.attempt == RECONNECT_ATTEMPTS
    assert ("connected", 0) in seen
    assert any(conn == "reconnecting" for conn, _ in seen)
    assert feed.fetches == 1 + RECONNECT_ATTEMPTS


def test_retrying_feed_stays_connecting_until_it_connects():
    feed = FlakyFeed(retry_connect=True, connect_fails=2)
    engine = Engine(feed, CFG, "XAUUSD")
    states = []

    def done():
        states.append(engine.state.conn)
        return engine.state.version > 0

    run_until(engine, done)
    assert engine.state.conn == "connected" and engine.state.error is None
    assert feed.connects == 3
    assert "connected" not in states[: states.index("connected")]  # never "done" before the first scan
    assert "connecting" in states


def test_mt5_reconnect_never_starts_a_closed_terminal(monkeypatch):
    mt5 = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    feed = MT5Feed("XAUUSD", path=r"C:\MT5 Broker\terminal64.exe")
    feed.connect()
    monkeypatch.setattr(mt5_terminals, "is_running", lambda path: False)
    mt5.fail_next_copy = 1
    with pytest.raises(RuntimeError, match="terminal is closed"):
        feed.fetch_m1(5)
    with pytest.raises(RuntimeError, match="terminal is closed"):
        feed.fetch_m1(5)  # still closed: not started again
    assert len(mt5.init_calls) == 1

    monkeypatch.setattr(mt5_terminals, "is_running", lambda path: True)
    assert len(feed.fetch_m1(5)) == 5  # reopened by the trader: attaches again
    assert len(mt5.init_calls) == 2


def test_normalize_terminal_path():
    assert mt5_terminals.normalize_path('  "C:\\MT5\\terminal64.exe" ') == "C:\\MT5\\terminal64.exe"
    assert mt5_terminals.normalize_path("C:\\XM MT5").endswith("terminal64.exe")
    assert mt5_terminals.normalize_path("  ") is None
    assert DataSettings.from_dict({"source": "mt5", "mt5_path": '"C:\\A\\terminal64.exe"'}).mt5_path == "C:\\A\\terminal64.exe"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def delete_password(self, service, account):
        self.store.pop((service, account))


def _api(tmp_path):
    runtime = Runtime(CFG, DataSettings(source="synthetic"), None)
    return runtime, TestClient(create_app(runtime, ui_dir=tmp_path))


def test_mt5_password_goes_to_the_credential_store(tmp_path, monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring", lambda: kr)
    runtime, api = _api(tmp_path)
    try:
        body = {"source": "synthetic", "mt5_login": 123, "mt5_server": "Broker-Live"}
        res = api.put("/api/settings", json={**body, "mt5_password": "s3cret"})
        assert res.status_code == 200
        assert "s3cret" not in res.text
        assert res.json()["mt5_password"] == "saved"
        assert kr.store == {("Wednesday MT5", "123@Broker-Live"): "s3cret"}
        assert runtime.mt5_password_for(runtime.settings) == ("s3cret", "saved")

        res = api.put("/api/settings", json={**body, "forget_mt5_password": True})
        assert res.json()["mt5_password"] is None and kr.store == {}
    finally:
        runtime.stop()


def test_mt5_password_without_credential_store_is_kept_for_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "_keyring", lambda: None)
    runtime, api = _api(tmp_path)
    try:
        res = api.put("/api/settings", json={"source": "synthetic", "mt5_login": 5, "mt5_password": "pw"})
        assert res.json()["mt5_password"] == "session"
        assert api.put("/api/settings", json={"source": "synthetic", "mt5_password": "pw"}).status_code == 422
    finally:
        runtime.stop()


def test_reconnect_and_terminals_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr("wednesday.server.find_terminals",
                        lambda: [{"path": "C:\\MT5\\terminal64.exe", "name": "MT5", "running": True}])
    runtime, api = _api(tmp_path)
    try:
        before = runtime.engine
        body = api.post("/api/settings/reconnect").json()
        assert runtime.engine is not before
        assert body["running"]["conn"] in {"connecting", "connected"}
        assert body["running"]["max_attempts"] is None  # demo data keeps retrying
        assert api.get("/api/status").json()["app_version"] == wednesday.__version__
        assert api.get("/api/mt5/terminals").json()["terminals"][0]["name"] == "MT5"
    finally:
        runtime.stop()
