import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app


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
        if t["bias"]:
            assert t["bias"]["direction"] in {"bullish", "bearish"}
            assert t["bias"]["event"] in {"BOS", "CHoCH"}
            assert t["bias"]["break_close_time"] > t["bias"]["break_time"]
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


def test_quarters_endpoint(tmp_path):
    from wednesday.engine import Runtime
    from wednesday.scanner import ScanConfig
    from wednesday.settings import DataSettings
    from wednesday.timeframes import TIMEFRAMES_BY_NAME

    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), None)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    assert api.get("/api/quarters").status_code == 503
    # A fixed weekday: on a weekend the last hours of bars hold no trading session.
    runtime.engine.feed = runtime.engine.buffer.feed = SyntheticFeed(end=pd.Timestamp("2026-03-04 12:00"))
    runtime.engine.feed.connect()
    runtime.engine.step()
    body = api.get("/api/quarters").json()
    assert body["clock"] == "UTC" and api.get("/api/status").json()["clock"] == "UTC"
    assert {"week", "session", "q90"} <= body["rows"].keys()
    assert body["rows"]["session"] and body["stats"]["week"][2]["label"] == "Wed"


def test_status_has_pip_and_clock_offset(engine, tmp_path):
    from wednesday import server
    from wednesday.sizing import pip_size

    engine.step()
    client = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path, clock="Etc/GMT-3"))
    body = client.get("/api/status").json()
    assert body["pip"] == pytest.approx(0.1)  # gold, no terminal spec
    assert body["clock_offset"] == 3 * 3600
    assert server.clock_offset("UTC") == 0
    # New York in summer is UTC-4, in winter UTC-5; "NY+7" is a broker clock 7 hours ahead of it.
    assert server.clock_offset("NY+7") in (3 * 3600, 2 * 3600)
    # The terminal's point decides it: ten points to a pip.
    assert pip_size("XAUUSD", 2400, {"point": 0.01}) == pytest.approx(0.1)
    assert pip_size("XAUUSD", 2400, {"point": 0.001}) == pytest.approx(0.01)
    assert pip_size("EURUSD", 1.1) == pytest.approx(0.0001)
    engine.state.spec = {"point": 0.001}
    assert client.get("/api/status").json()["pip"] == pytest.approx(0.01)
