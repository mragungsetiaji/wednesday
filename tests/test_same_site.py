"""No login, but no cross-site writes: another web page in the browser can't drive the dashboard."""

import argparse

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday import cli
from wednesday.detectors import DetectorParams
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.same_site import cross_site
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.storage import Store


@pytest.fixture
def engine(tmp_path):
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=pd.Timestamp("2026-03-02 12:00"))
    return Engine(feed, cfg, "XAUUSD", Store(f"sqlite:///{tmp_path / 'x.db'}"))


def test_cross_site_writes_are_refused(engine, tmp_path):
    client = TestClient(create_app(engine, ui_dir=tmp_path))
    evil = client.put("/api/bias", json={}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and evil.json() == {"detail": "Cross-site request refused"}
    assert client.put("/api/bias", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.put("/api/bias", json={}, headers={"Origin": "http://testserver"}).status_code != 403
    assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 200  # reads


def test_cross_site_rules():
    assert not cross_site("GET", {"origin": "https://evil.example", "host": "a"})
    assert cross_site("POST", {"origin": "null", "host": "a"})
    assert not cross_site("POST", {"origin": "https://dash.example", "host": "dash.example"})
    assert not cross_site("POST", {"origin": "https://dash.example", "host": "127.0.0.1:8000",
                                   "x-forwarded-host": "dash.example"})
    assert not cross_site("POST", {"host": "a"})  # scripts and curl send no Origin
    assert not cross_site("POST", {"host": "a", "sec-fetch-site": "same-origin"})


def test_there_is_no_login_on_loopback(engine, tmp_path):
    client = TestClient(create_app(engine, ui_dir=tmp_path))
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json() == {"login": False, "logged_in": False, "setup": False,
                                                     "user": None, "signup": False, "guest": None,
                                                     "guest_available": False}
    assert client.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 409


def test_the_login_is_on_for_a_network_host():
    args = lambda host, insecure=False, login=False: argparse.Namespace(  # noqa: E731
        host=host, insecure=insecure, login=login)
    for host in ("127.0.0.1", "localhost", "::1"):
        assert not cli.login_on(args(host))
    assert cli.login_on(args("127.0.0.1", login=True))
    assert cli.login_on(args("0.0.0.0"))
    assert not cli.login_on(args("0.0.0.0", insecure=True))
