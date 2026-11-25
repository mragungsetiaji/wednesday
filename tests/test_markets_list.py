"""The symbol dropdown's markets list: seeded by migration 2, edited in Settings > Markets."""

import pytest
from fastapi.testclient import TestClient

from wednesday.markets import clean_market
from wednesday.storage import DEFAULT_MARKETS, Store


def test_the_list_is_seeded_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'w.db'}"
    store = Store(url)
    assert [m["symbol"] for m in store.markets()] == [m[0] for m in DEFAULT_MARKETS]
    store.delete_market("TVC:DXY")
    assert "TVC:DXY" not in [m["symbol"] for m in Store(url).markets()]  # not seeded again


def test_save_reorders_and_add_goes_last(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    rows = list(reversed(store.markets()))
    rows[0] = {**rows[0], "label": "Dow", "keep_live": True}
    store.save_markets(rows)
    first = store.markets()[0]
    assert (first["symbol"], first["label"], first["keep_live"]) == ("OANDA:US30USD", "Dow", True)
    store.add_market({"symbol": "FX:EURUSD", "label": "EUR", "name": "Euro", "venue": "FXCM"})
    assert store.markets()[-1]["symbol"] == "FX:EURUSD"


def test_clean_market_says_what_is_wrong():
    with pytest.raises(ValueError, match="TradingView symbol"):
        clean_market({"symbol": "XAUUSD", "label": "x"})
    with pytest.raises(ValueError, match="label"):
        clean_market({"symbol": "FX:XAUUSD", "label": " "})
    with pytest.raises(ValueError, match="longer"):
        clean_market({"symbol": "FX:XAUUSD", "label": "x" * 17})
    assert clean_market({"symbol": "fx:xauusd", "label": "XAU"})["symbol"] == "FX:XAUUSD"


def client(tmp_path, monkeypatch):
    import pandas as pd

    from wednesday.detectors import DetectorParams
    from wednesday.engine import Engine
    from wednesday.feeds import SyntheticFeed
    from wednesday.markets import MarketError, Markets
    from wednesday.scanner import ScanConfig
    from wednesday.server import create_app

    def info(self, symbol):
        if symbol == "FX:NOPE":
            raise MarketError(404, "TradingView has no symbol FX:NOPE")
        return {"description": "Euro / US Dollar", "exchange": "FXCM", "pricescale": 100000}

    monkeypatch.setattr(Markets, "info", info)
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(history=500, end=pd.Timestamp("2026-03-02 12:00"))
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    return TestClient(create_app(Engine(feed, cfg, "FX:XAUUSD", store), source="synthetic", ui_dir=tmp_path))


def test_the_markets_api(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    listed = c.get("/api/markets").json()["markets"]
    assert listed[0] == {"symbol": "FX:XAUUSD", "label": "XAU", "name": "Gold spot", "venue": "FXCM",
                         "position": 0, "keep_live": False}

    added = c.post("/api/markets", json={"symbol": "fx:eurusd"}).json()["markets"][-1]
    assert (added["symbol"], added["label"], added["name"], added["venue"]) == \
        ("FX:EURUSD", "EURUSD", "Euro / US Dollar", "FXCM")
    assert c.post("/api/markets", json={"symbol": "FX:EURUSD"}).status_code == 409
    assert c.post("/api/markets", json={"symbol": "EURUSD"}).status_code == 400
    assert c.post("/api/markets", json={"symbol": "FX:NOPE"}).status_code == 404

    rows = c.get("/api/markets").json()["markets"]
    rows = [{**rows[-1], "label": "EUR", "keep_live": True}, *rows[:-1]]
    saved = c.put("/api/markets", json={"markets": rows}).json()["markets"]
    assert (saved[0]["symbol"], saved[0]["label"], saved[0]["keep_live"]) == ("FX:EURUSD", "EUR", True)
    assert c.put("/api/markets", json={"markets": rows[:-1]}).status_code == 422

    assert c.delete("/api/markets", params={"symbol": "FX:XAUUSD"}).status_code == 409  # being scanned
    assert c.delete("/api/markets", params={"symbol": "FX:GBPUSD"}).status_code == 404
    left = c.delete("/api/markets", params={"symbol": "FX:EURUSD"}).json()["markets"]
    assert "FX:EURUSD" not in [m["symbol"] for m in left]
