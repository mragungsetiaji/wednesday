import time
from importlib.metadata import EntryPoint

from fastapi.testclient import TestClient

import plugin_fixture
from wednesday import plugins, server
from wednesday.engine import Runtime
from wednesday.plugins import PLUGIN_API, Hooks
from wednesday.scanner import ScanConfig
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME


def ep(name, attr):
    return EntryPoint(name=name, value=f"plugin_fixture:{attr}", group="wednesday.plugins")


EPS = [ep("ee", "good"), ep("future", "future"), ep("broken", "broken"), ep("junk", "not_a_plugin"),
       ep("missing", "nope")]


def make(tmp_path, monkeypatch, eps=EPS):
    monkeypatch.setattr(server, "load_plugins", lambda app, rt: plugins.load_plugins(app, rt, eps))
    store = Store(f"sqlite:///{tmp_path / 'p.db'}")
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store)
    return TestClient(server.create_app(runtime, ui_dir=tmp_path)), runtime


def test_plugins_load_and_bad_ones_are_skipped(tmp_path, monkeypatch):
    api, runtime = make(tmp_path, monkeypatch)
    body = api.get("/api/plugins").json()
    by = {p["name"]: p for p in body["plugins"]}
    assert body["api"] == PLUGIN_API
    assert by["ee"]["loaded"] and by["ee"]["version"] == "0.1.0"
    assert set(by["ee"]["features"]) == {"lab.signed_models", "llm.recap"}
    assert not by["future"]["loaded"] and "API 99" in by["future"]["error"]
    assert not by["broken"]["loaded"] and "licence server down" in by["broken"]["error"]
    assert not by["broken"]["features"]
    assert "register" in by["junk"]["error"]
    assert "ModuleNotFoundError" in by["missing"]["error"] or "AttributeError" in by["missing"]["error"]
    assert body["features"] == ["lab.signed_models", "llm.recap"]
    # Routes live under /api/ee/<name>, and the rest of the app still works.
    assert api.get("/api/ee/ee/hello").json()["hello"] == "ee"
    assert api.get("/api/status").status_code == 200
    runtime.stop()


def test_hooks_run_after_each_scan_and_failures_are_isolated(tmp_path, monkeypatch):
    plugin_fixture.calls.update(after_scan=[], on_alert=[], job=[])
    api, runtime = make(tmp_path, monkeypatch)
    result = runtime.engine.step()
    runtime._after_scan(result)  # the failing hook raises inside; nothing escapes
    assert plugin_fixture.calls["after_scan"] == [runtime.engine.symbol]
    runtime.hooks.run_on_alert(["k1"], result)
    runtime.hooks.run_on_alert([], result)  # nothing sent: not called
    assert plugin_fixture.calls["on_alert"] == [["k1"]]
    deadline = time.time() + 5
    while not plugin_fixture.calls["job"] and time.time() < deadline:
        time.sleep(0.1)
    assert plugin_fixture.calls["job"]
    runtime.stop()
    n = len(plugin_fixture.calls["job"])
    time.sleep(1.5)
    assert len(plugin_fixture.calls["job"]) <= n + 1  # jobs stop on shutdown (one run may be in flight)


def test_no_plugins_is_fine(tmp_path, monkeypatch):
    api, runtime = make(tmp_path, monkeypatch, eps=[])
    assert api.get("/api/plugins").json() == {"api": PLUGIN_API, "plugins": [], "features": []}
    assert api.get("/api/ee/ee/hello").status_code == 404


def test_add_job_refuses_busy_loops():
    import pytest

    from wednesday.plugins import PluginContext, PluginInfo

    class RT:
        hooks = Hooks()

    ctx = PluginContext(None, RT(), PluginInfo("x", None, 1, [], False))
    with pytest.raises(ValueError):
        ctx.add_job("fast", 0.1, lambda: None)
