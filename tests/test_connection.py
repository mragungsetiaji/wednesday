"""Feed connection states, and forgetting saved secrets (the installer's fresh install)."""

import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import wednesday
from wednesday import engine as engine_mod
from wednesday import secret_store
from wednesday.detectors import DetectorParams
from wednesday.engine import RECONNECT_ATTEMPTS, Engine, Runtime
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings

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
            raise RuntimeError("feed not logged in")

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
    assert feed.connects == 1  # a feed that may not retry connects once only
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


def test_forget_all_clears_saved_secrets(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring", lambda: kr)
    name = next(iter(secret_store.APP_SECRETS))
    assert secret_store.set_secret(name, "s3cret") == "saved"
    kr.store[("Someone else", "x")] = "theirs"
    assert secret_store.forget_all() == 1
    assert kr.store == {("Someone else", "x"): "theirs"}
    monkeypatch.setattr(secret_store, "_keyring", lambda: None)
    secret_store.set_secret(name, "session only")
    secret_store.forget_all()
    assert secret_store.recall(secret_store.SERVICE, name) == (None, None)


def test_reconnect_endpoint(tmp_path):
    runtime, api = _api(tmp_path)
    try:
        before = runtime.engine
        body = api.post("/api/settings/reconnect").json()
        assert runtime.engine is not before
        assert body["running"]["conn"] in {"connecting", "connected"}
        assert body["running"]["max_attempts"] is None  # demo data keeps retrying
        assert api.get("/api/status").json()["app_version"] == wednesday.__version__
    finally:
        runtime.stop()
