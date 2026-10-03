"""Journal entries (#14): notes, session reviews and setups, with the market frozen when written."""

import pytest
from fastapi.testclient import TestClient

from wednesday import server
from wednesday.bias import TradeBias
from wednesday.engine import Runtime
from wednesday.journal.market import freeze
from wednesday.journal.service import Journals
from wednesday.plugins import PluginInfo
from wednesday.scanner import ScanConfig
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 'j.db'}")
    runtime = Runtime(ScanConfig(lookback=40, timeframes=(TIMEFRAMES_BY_NAME["5M"], TIMEFRAMES_BY_NAME["1H"])),
                      DataSettings(source="synthetic"), store, journals=Journals(store))
    runtime.engine.feed.connect()
    runtime.engine.step()
    runtime.set_bias(TradeBias("bearish", note="CPI hot").stamped())
    monkeypatch.setattr(server, "load_plugins", lambda app, rt: [PluginInfo("fake", None, 1, [], loaded=True)])
    client = TestClient(server.create_app(runtime, ui_dir=tmp_path))
    client.runtime = runtime
    return client


def test_notes_reviews_and_setups_keep_the_market_they_were_written_in(api):
    j = api.post("/api/journals", json={"name": "Main"}).json()
    base = f"/api/journals/{j['id']}/entries"
    note = api.post(base, json={"kind": "review", "text": "London swept Asia high, waited for NY", "tags": ["Patience", "patience"],
                                "mood": 4}).json()
    assert note["kind"] == "review" and note["tags"] == ["patience"] and note["mood"] == 4
    m = note["market"]
    assert m["bias"]["direction"] == "bearish" and m["symbol"] == "XAUUSD" and m["price"]
    assert {t["timeframe"] for t in m["structure"]} <= {"5M", "1H"}
    setup = {"tag": "S1", "timeframe": "5M", "side": "sell", "entry": 2450.5, "sl": 2453.0}
    s = api.post(base, json={"kind": "setup", "setup": setup, "text": ""}).json()
    assert s["setup"] == setup

    frozen = api.get(base).json()["entries"][-1]["market"]
    api.runtime.set_bias(None)  # the market moves on
    api.runtime.engine.step()
    again = next(e for e in api.get(base).json()["entries"] if e["id"] == note["id"])
    assert again["market"] == frozen  # still what it was

    edited = api.patch(f"{base}/{note['id']}", json={"text": "Waited for NY AM", "mood": 5}).json()
    assert edited["text"] == "Waited for NY AM" and edited["market"] == frozen and edited["updated_at"]
    assert [e["kind"] for e in api.get(base).json()["entries"]] == ["setup", "review"]  # newest first
    assert api.delete(f"{base}/{s['id']}").json() == {"deleted": True}


def test_entries_are_checked_and_go_with_the_journal(api):
    j = api.post("/api/journals", json={"name": "Main"}).json()
    base = f"/api/journals/{j['id']}/entries"
    assert api.post(base, json={"kind": "note", "text": "  "}).status_code == 422
    assert api.post(base, json={"kind": "diary", "text": "x"}).status_code == 422
    assert api.post(base, json={"kind": "note", "text": "x", "mood": 9}).status_code == 422
    assert api.post(base, json={"kind": "setup"}).status_code == 422
    assert api.patch(f"{base}/nope", json={"text": "x"}).status_code == 404
    api.post(base, json={"kind": "note", "text": "x"})
    api.delete(f"/api/journals/{j['id']}")
    assert api.runtime.store.journal_entries(j["id"]) == []


def test_freeze_keeps_only_the_nearest_setups_and_coming_news():
    scan = {"time": "2026-03-04T10:00:00", "price": 2400.0, "timeframes": [],
            "setups": {"sell": [{"timeframe": "5M", "meta": {"entry": 2400 + i, "sl": 2405 + i}} for i in range(5)],
                       "buy": []}}
    news = [{"time": "2026-03-04T13:30:00+00:00", "currency": "USD", "title": "CPI", "impact": "High"},
            {"time": "2026-03-01T13:30:00+00:00", "currency": "USD", "title": "old", "impact": "High"}]
    from datetime import datetime, timezone

    f = freeze(scan, None, news, "XAUUSD", "mt5", now=datetime(2026, 3, 4, 10, tzinfo=timezone.utc))
    assert len(f["setups"]["sell"]) == 3 and f["setups"]["sell"][0]["entry"] == 2400
    assert [n["title"] for n in f["news"]] == ["CPI"] and f["bias"] is None
