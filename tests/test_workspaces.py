"""Named workspaces: windows and chart layout, saved in the database (#27)."""

import pandas as pd
from fastapi.testclient import TestClient

from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.storage import Store
from wednesday.workspaces import MAX_WORKSPACES

WIN = [{"route": "#", "x": 0, "y": 0, "width": 1440, "height": 900, "maximized": True},
       {"route": "#popout/4H/A", "x": 1920, "y": 0, "width": 1100, "height": 700}]


def client(tmp_path):
    engine = Engine(SyntheticFeed(), ScanConfig(lookback=20), "XAUUSD", store=Store(f"sqlite:///{tmp_path / 'w.db'}"))
    return TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))


def test_save_list_open_delete(tmp_path):
    api = client(tmp_path)
    layout = {"wed.focusLayout": "4", "xau.tf": "15M", "secret.token": "x"}
    saved = api.put("/api/workspaces/London prep", json={"layout": layout, "windows": WIN}).json()
    assert saved["layout"] == {"wed.focusLayout": "4", "xau.tf": "15M"}  # only the dashboard's layout keys
    assert saved["windows"][1]["route"] == "#popout/4H/A" and saved["windows"][0]["maximized"] is True
    assert api.get("/api/workspaces").json()["workspaces"][0]["name"] == "London prep"
    assert api.get("/api/workspaces/London prep").json()["layout"]["xau.tf"] == "15M"
    assert api.delete("/api/workspaces/London prep").json() == {"deleted": True}
    assert api.get("/api/workspaces/London prep").status_code == 404


def test_bad_input_is_refused(tmp_path):
    api = client(tmp_path)
    bad = [{"route": "https://evil.example", "x": 0, "y": 0, "width": 800, "height": 600}]
    assert api.put("/api/workspaces/x", json={"windows": bad}).status_code == 422
    assert api.put("/api/workspaces/x", json={"windows": [{"route": "#", "x": "a"}]}).status_code == 422
    assert api.put("/api/workspaces/%20", json={}).status_code == 422
    for i in range(MAX_WORKSPACES):
        assert api.put(f"/api/workspaces/w{i}", json={}).status_code == 200
    assert "remove one" in api.put("/api/workspaces/one more", json={}).json()["detail"]
