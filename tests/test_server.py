import pandas as pd
import pytest
from fastapi.testclient import TestClient

from xau_screener.detectors import DetectorParams
from xau_screener.engine import Engine
from xau_screener.feeds import SyntheticFeed
from xau_screener.scanner import ScanConfig
from xau_screener.server import create_app


@pytest.fixture
def engine():
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=pd.Timestamp("2026-03-02 12:00"))
    return Engine(feed, cfg, "XAUUSD")


def test_endpoints_before_first_scan(engine, tmp_path):
    client = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))
    body = client.get("/api/scan").json()
    assert body["version"] == 0 and body["scan"] is None
    assert body["config"]["timeframes"] == ["4H", "1H", "30M", "15M", "5M"]
    assert client.get("/api/candles?tf=1H").status_code == 503
    assert "Dashboard not built" in client.get("/").text


def test_scan_and_candles(engine, tmp_path):
    engine.step()
    client = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))
    body = client.get("/api/scan").json()
    assert body["version"] == 1 and body["error"] is None
    scan = body["scan"]
    assert [t["timeframe"] for t in scan["timeframes"]] == ["4H", "1H", "30M", "15M", "5M"]
    assert [d["name"] for d in body["config"]["detectors"]] == ["ob", "liquidity", "idm"]
    assert body["config"]["swing_length"] == 3
    for t in scan["timeframes"]:
        assert set(t["detectors"]) == {"ob", "liquidity", "idm"}
        for d in t["detectors"].values():
            assert d["active_count"] == len(d["active"])
    above = scan["nearest"]["ob"]["above"]
    if above:
        assert above["bottom"] > scan["price"]
        assert above["timeframe"] in {"4H", "1H", "30M", "15M", "5M"}
        assert isinstance(above["time_unix"], int)

    res = client.get("/api/candles?tf=15m&limit=50").json()
    assert res["timeframe"] == "15M"
    assert len(res["candles"]) == 50
    times = [c["time"] for c in res["candles"]]
    assert times == sorted(times)
    tf15 = next(t for t in scan["timeframes"] if t["timeframe"] == "15M")
    assert len(res["levels"]) == sum(d["active_count"] for d in tf15["detectors"].values())
    assert {lv["detector"] for lv in res["levels"]} <= {"ob", "liquidity", "idm"}
    assert client.get("/api/candles?tf=2H").status_code == 404


def test_feed_error_is_reported(engine, tmp_path):
    engine.step()

    def boom(count):
        raise RuntimeError("terminal closed")

    engine.feed.fetch_m1 = boom
    with pytest.raises(RuntimeError):
        engine.step()
    body = TestClient(create_app(engine, ui_dir=tmp_path)).get("/api/scan").json()
    assert "terminal closed" in body["error"]
    assert body["scan"] is not None  # last good scan is kept


def test_serves_built_ui(engine, tmp_path):
    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    client = TestClient(create_app(engine, ui_dir=tmp_path))
    assert "dashboard" in client.get("/").text
