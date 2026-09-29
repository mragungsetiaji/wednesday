import pytest
from fastapi.testclient import TestClient

from wednesday import server
from wednesday.engine import Runtime
from wednesday.journal import sample
from wednesday.journal.service import SAMPLE_ID, Journals
from wednesday.journal.stats import analyse
from wednesday.plugins import PluginInfo
from wednesday.scanner import ScanConfig
from wednesday.settings import DataSettings
from wednesday.storage import Store, journal_cash_table, journal_trades_table
from wednesday.timeframes import TIMEFRAMES_BY_NAME


@pytest.fixture(scope="module")
def generated():
    return sample.generate()


def test_the_generator_is_deterministic_and_every_deal_is_in_its_bar(generated):
    again = sample.generate()
    assert again["trades"] == generated["trades"] and again["cash"] == generated["cash"]
    assert again["bars"].equals(generated["bars"])
    a = analyse(generated["trades"], generated["cash"], {sample.SYMBOL: generated["bars"]})
    b = analyse(again["trades"], again["cash"], {sample.SYMBOL: again["bars"]})
    assert a["summary"] == b["summary"] and a["trading"] == b["trading"]
    v = a["verification"]
    assert v["prices_checked"] == 2 * len(generated["trades"]) == v["prices_ok"]
    assert v["verified"] == len(generated["trades"]) and v["offset_hours"] == 0
    assert sample.generate(seed=2)["trades"] != generated["trades"]


def test_the_sample_fills_every_panel(generated):
    got = analyse(generated["trades"], generated["cash"], {sample.SYMBOL: generated["bars"]})
    s = got["summary"]
    assert s["deposits"] > 0 and s["withdrawals"] > 0
    assert len(got["monthly"]) >= 5 and any(m["gain"] < 0 for m in got["monthly"])
    assert any(m["gain"] > 0 for m in got["monthly"])
    assert len(got["daily"]) > 60 and got["trading"]["trades"] > 3 * 25  # several pages of history
    assert {t["side"] for t in got["trades"]} == {"buy", "sell"}
    # Trades that sat deep in loss and still closed green: why the drawdown comes from the price.
    assert sum(1 for t in got["trades"] if t["net"] > 0 and t["mae"] < -t["net"]) >= 5
    closed_only = analyse(generated["trades"], generated["cash"], {})["summary"]["drawdown"]
    assert s["drawdown"] > closed_only + 0.5


def test_the_sample_journal_is_free_and_deletable(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'j.db'}")
    js = Journals(store, sample=True)
    [j] = js.list()
    assert j["id"] == SAMPLE_ID and j["sample"] is True and j["login"] is None
    assert js.list() == [j]  # made once
    st = js.stats(SAMPLE_ID)
    assert st["verification"]["prices_from"] == {"XAUUSD": "sample:XAUUSD"}

    # A free user keeps the sample and makes one journal of their own; a second one needs the plan.
    own = js.create("Main", [])
    with pytest.raises(PermissionError):
        js.create("Second", [])
    # The sample can't be synced or imported into.
    with pytest.raises(ValueError, match="made-up"):
        js.sync(SAMPLE_ID, {"account": {"login": "1"}, "deals": [], "positions": []})
    with pytest.raises(ValueError, match="made-up"):
        js.import_report(SAMPLE_ID, b"<html></html>")

    # A real journal with gold trades on the sample's dates never reads the sample's bars.
    t0 = st["trades"][0]
    store.journal_fill(own["id"], [{k: t0[k] for k in ("id", "position", "symbol", "side", "volume", "open_time",
                                                      "open_price", "close_time", "close_price", "profit",
                                                      "commission", "swap", "sl", "tp", "comment")}], [], replace=True)
    mine = js.stats(own["id"])["verification"]
    assert mine["prices_from"] == {} and mine["no_prices"] == ["XAUUSD"]

    assert js.delete(SAMPLE_ID) is True
    assert store.journal_rows(journal_trades_table, SAMPLE_ID) == []
    assert store.journal_rows(journal_cash_table, SAMPLE_ID) == []
    assert store.bar_bounds(sample.SOURCE, sample.SYMBOL)[0] == 0
    # It stays gone after a restart.
    assert [x["id"] for x in Journals(store, sample=True).list()] == [own["id"]]
    back = Journals(store, sample=True).restore_sample()
    assert back["sample"] is True and store.bar_bounds(sample.SOURCE, sample.SYMBOL)[0] == len(sample.generate()["bars"])


def test_sample_api(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 'j.db'}")
    runtime = Runtime(ScanConfig(lookback=40, timeframes=(TIMEFRAMES_BY_NAME["5M"],)), DataSettings(source="synthetic"),
                      store, journals=Journals(store, sample=True))
    monkeypatch.setattr(server, "load_plugins", lambda app, rt: [PluginInfo("fake", None, 1, [], loaded=True)])
    api = TestClient(server.create_app(runtime, ui_dir=tmp_path))
    [j] = api.get("/api/journals").json()["journals"]
    assert j["sample"] is True
    for path in ("sync", "import"):
        r = api.post(f"/api/journals/{SAMPLE_ID}/{path}", content=b"x")
        assert r.status_code == 409 and "made-up" in r.json()["detail"]
    assert api.post("/api/journals", json={"name": "Main"}).status_code == 200
    assert api.delete(f"/api/journals/{SAMPLE_ID}").json() == {"deleted": True}
    assert [x["name"] for x in api.get("/api/journals").json()["journals"]] == ["Main"]
    assert api.post("/api/journals/sample").json()["name"] == "Sample portfolio"
    assert len(api.get("/api/journals").json()["journals"]) == 2
