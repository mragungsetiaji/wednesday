"""Chart drawings: validation, storage per source and symbol, and the API."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.drawings import clean_drawing
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.storage import Store

ID = "0f6c1d2e-8b4a-4c7e-9a51-3d2f7e6b1c90"
LINE = {"kind": "trendline", "points": [{"t": 1790484000, "p": 2470.5}, {"t": 1790487600, "p": 2480}],
        "style": {"color": None, "width": 2}}


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'xau.db'}")


def app_for(store, tmp_path, symbol="XAUUSD"):
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(history=500, end=pd.Timestamp("2026-03-02 12:00"))
    return TestClient(create_app(Engine(feed, cfg, symbol, store), source="synthetic", ui_dir=tmp_path))


def test_clean_drawing_checks_kind_points_and_style():
    row = clean_drawing(ID, LINE)
    assert row["points"] == [{"t": 1790484000, "p": 2470.5}, {"t": 1790487600, "p": 2480.0}]
    assert row["style"] == {"color": None, "width": 2, "dash": "solid"} and row["locked"] is False
    assert clean_drawing(ID, {"kind": "hline", "points": [{"t": 1, "p": 2}]})["style"]["width"] == 1
    for bad in (
        {**LINE, "kind": "circle"},
        {**LINE, "points": LINE["points"][:1]},  # a trendline takes two points
        {**LINE, "points": [{"t": 1}, {"t": 2, "p": 3}]},
        {**LINE, "points": [{"t": 1, "p": "nan"}, {"t": 2, "p": 3}]},
        {**LINE, "style": {"color": "red"}},
        {**LINE, "style": {"width": 9}},
        {**LINE, "style": {"dash": "wavy"}},
    ):
        with pytest.raises(ValueError):
            clean_drawing(ID, bad)
    with pytest.raises(ValueError):
        clean_drawing("../etc", LINE)


def test_positions_need_stop_and_target_on_their_sides():
    pts = [{"t": 1, "p": 2470}, {"t": 2, "p": 2471}]
    long = clean_drawing(ID, {"kind": "long", "points": pts, "props": {"stop": 2465, "target": "2480"}})
    assert long["props"] == {"stop": 2465.0, "target": 2480.0}
    assert long["points"][1]["p"] == 2470  # the end sits at the entry
    short = clean_drawing(ID, {"kind": "short", "points": pts, "props": {"stop": 2475, "target": 2460}})
    assert short["props"]["stop"] == 2475
    for kind, props in (("long", None), ("long", {"stop": 2475, "target": 2480}), ("long", {"stop": 2465, "target": 2460}),
                        ("short", {"stop": 2465, "target": 2460}), ("short", {"stop": "x", "target": 2460})):
        with pytest.raises(ValueError):
            clean_drawing(ID, {"kind": kind, "points": pts, "props": props})


def test_paths_texts_and_styles():
    pts = [{"t": i, "p": 2400 + i} for i in range(5)]
    path = clean_drawing(ID, {"kind": "path", "points": pts, "style": {"color": "#22c55e", "width": 3, "dash": "dotted"}})
    assert len(path["points"]) == 5 and path["style"] == {"color": "#22c55e", "width": 3, "dash": "dotted"}
    assert path["props"] is None  # stray props are dropped
    text = clean_drawing(ID, {"kind": "text", "points": pts[:1], "props": {"text": "NY open"}, "style": {"size": 16}})
    assert text["props"] == {"text": "NY open"} and text["style"]["size"] == 16
    for bad in (
        {"kind": "path", "points": pts[:1]},
        {"kind": "path", "points": [{"t": i, "p": 1} for i in range(201)]},
        {"kind": "text", "points": pts[:1], "props": {"text": "  "}},
        {"kind": "text", "points": pts[:1], "props": {"text": "x" * 501}},
        {"kind": "text", "points": pts[:1], "props": {"text": "ok"}, "style": {"size": 99}},
    ):
        with pytest.raises(ValueError):
            clean_drawing(ID, bad)


def test_store_keeps_drawings_per_source_and_symbol(store):
    row = clean_drawing(ID, LINE)
    store.drawing_put({**row, "source": "mt5", "symbol": "XAUUSD"})
    assert store.drawings("mt5", "XAUUSD")[0]["points"] == row["points"]
    assert store.drawings("yfinance", "GC=F") == []
    store.drawing_put({**row, "locked": True, "source": "mt5", "symbol": "XAUUSD"})  # upsert
    [saved] = store.drawings("mt5", "XAUUSD")
    assert saved["locked"] is True and saved["props"] is None and saved["timeframes"] is None
    assert not store.drawing_delete(ID, "yfinance", "GC=F")  # another symbol's delete misses
    assert store.drawing_delete(ID, "mt5", "XAUUSD") and store.drawings("mt5", "XAUUSD") == []


def test_drawings_api(store, tmp_path):
    client = app_for(store, tmp_path)
    assert client.get("/api/drawings").json() == {"source": "synthetic", "symbol": "XAUUSD", "drawings": []}
    saved = client.put(f"/api/drawings/{ID}", json=LINE).json()
    assert saved["id"] == ID and saved["kind"] == "trendline"
    [listed] = client.get("/api/drawings").json()["drawings"]
    assert listed["points"] == saved["points"] and "source" not in listed
    assert client.put(f"/api/drawings/{ID}", json={**LINE, "kind": "circle"}).status_code == 422
    assert app_for(store, tmp_path, symbol="GOLD").get("/api/drawings").json()["drawings"] == []
    assert client.delete(f"/api/drawings/{ID}").status_code == 200
    assert client.delete(f"/api/drawings/{ID}").status_code == 404


def test_drawings_need_a_database(tmp_path):
    client = app_for(None, tmp_path)
    assert client.get("/api/drawings").status_code == 409
