"""Drawing templates: validation, storage in the settings table, and the API."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.drawing_templates import DrawingTemplates, TemplateError, clean_template
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.storage import Store

H1_OB = {"style": {"color": "#3b82f6", "width": 1, "dash": "dotted"}, "text": "H1 OB"}


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'xau.db'}")


def test_clean_template_keeps_the_look_only():
    t = clean_template({**H1_OB, "points": [{"t": 1, "p": 2}], "style": {**H1_OB["style"], "size": 14}})
    assert t["style"] == {"color": "#3b82f6", "width": 1, "dash": "dotted", "size": 14}
    assert t["text"] == "H1 OB" and "points" not in t
    assert clean_template({"style": {}, "text": "  "})["text"] is None
    for bad in ({}, {"style": {"color": "blue"}}, {"style": {"width": 9}}, {"style": {"dash": "wavy"}},
                {"style": {"size": 2}}, {"style": {}, "text": 5}):
        with pytest.raises(TemplateError):
            clean_template(bad)


def test_templates_by_family_sorted_and_replaced(store):
    tpl = DrawingTemplates(store)
    tpl.save("line", "m15 OB", H1_OB)
    tpl.save("line", "H1 OB", H1_OB)
    tpl.save("rect", "Zone", {"style": {"color": None, "width": 2}})
    tpl.save("line", "H1 OB", {**H1_OB, "text": "H1 OB (2)"})  # same name: replaced
    names = [(t["family"], t["name"], t["text"]) for t in tpl.list()]
    assert names == [("line", "H1 OB", "H1 OB (2)"), ("line", "m15 OB", "H1 OB"), ("rect", "Zone", None)]
    assert tpl.delete("line", "m15 OB") and not tpl.delete("line", "m15 OB")
    with pytest.raises(TemplateError):
        tpl.save("circle", "x", H1_OB)
    with pytest.raises(TemplateError):
        tpl.save("line", "   ", H1_OB)


def test_drawing_templates_api(store, tmp_path):
    feed = SyntheticFeed(history=500, end=pd.Timestamp("2026-03-02 12:00"))
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    client = TestClient(create_app(Engine(feed, cfg, "XAUUSD", store), source="synthetic", ui_dir=tmp_path))
    r = client.put("/api/drawing-templates/line/M15 SNR + FVG", json=H1_OB)
    assert r.status_code == 200 and r.json()["name"] == "M15 SNR + FVG"
    assert [t["name"] for t in client.get("/api/drawing-templates").json()["templates"]] == ["M15 SNR + FVG"]
    assert client.put("/api/drawing-templates/line/x", json={"style": {"width": 0}}).status_code == 422
    assert client.delete("/api/drawing-templates/line/M15 SNR + FVG").status_code == 200
    assert client.delete("/api/drawing-templates/line/M15 SNR + FVG").status_code == 404
