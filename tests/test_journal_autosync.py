"""Journal settings and MT5 auto-sync (issues #40, #42): a fake feed, no terminal."""

import pandas as pd
import pytest

from wednesday.journal.autosync import AutoSync
from wednesday.journal.imports import BALANCE, BUY, IN, OUT, SELL
from wednesday.journal.service import JournalError, Journals
from wednesday.storage import Store

T0 = int(pd.Timestamp("2026-03-02 00:00").timestamp())


def deal(ticket, t, kind, entry=IN, position=0, volume=0.0, price=0.0, profit=0.0):
    return {"ticket": ticket, "time": t, "time_msc": t * 1000, "type": kind, "entry": entry, "position_id": position,
            "symbol": "XAUUSD" if kind in (BUY, SELL) else "", "volume": volume, "price": price, "profit": profit,
            "commission": 0.0, "fee": 0.0, "swap": 0.0, "comment": ""}


class FakeFeed:
    """The two MT5 calls auto-sync uses, counting the full history reads."""

    def __init__(self, login="51234567"):
        self.login = login
        self.deals = [deal(1, T0, BALANCE, profit=10_000)]
        self.positions: list[dict] = []
        self.history_calls = 0
        self.fail = False

    def account_activity(self):
        return {"login": self.login, "deals": len(self.deals), "positions": len(self.positions)}

    def account_history(self):
        self.history_calls += 1
        if self.fail:
            raise RuntimeError("The MT5 terminal isn't connected yet")
        return {"account": {"login": self.login, "server": "Demo", "company": "Broker", "currency": "USD",
                            "balance": 10_000.0, "equity": 10_000.0},
                "deals": list(self.deals), "positions": list(self.positions)}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def journals(tmp_path):
    return Journals(Store(f"sqlite:///{tmp_path / 'j.db'}"))


def test_settings_persist_and_default(journals, tmp_path):
    j = journals.create("Live", [], "51234567", auto_sync=True)
    assert j["auto_sync"] is True and j["show_weekends"] is False and j["settings"] is None
    journals.update(j["id"], {"show_weekends": True, "auto_sync": False})
    again = Journals(Store(f"sqlite:///{tmp_path / 'j.db'}")).list()[0]  # survives a restart
    assert again["show_weekends"] is True and again["auto_sync"] is False
    assert again["sync"]["state"] == "off"
    with pytest.raises(JournalError):
        journals.update(j["id"], {"auto_sync": "yes"})


def test_auto_sync_defaults_from_source(journals):
    j = journals.create("Old", [], "1")
    assert j["auto_sync"] is False  # not made from the terminal's account
    row = journals.get(j["id"])
    row["settings"], row["source"] = None, "mt5"  # an older journal filled from MT5
    journals.store.journal_put(row)
    assert journals.list()[0]["auto_sync"] is True


def test_full_sync_only_when_something_changed(journals):
    j = journals.create("Live", [], "51234567", auto_sync=True)
    feed, clock = FakeFeed(), Clock()
    auto = AutoSync(journals, clock=clock)
    auto.check(feed)
    assert feed.history_calls == 1  # first look after connecting
    assert journals.list()[0]["sync"]["state"] == "ok"
    for _ in range(5):
        clock.t += 60
        auto.check(feed)
    assert feed.history_calls == 1  # nothing new, no full sync
    feed.deals += [deal(2, T0 + 60, BUY, IN, 7, 1.0, 2000.0), deal(3, T0 + 600, SELL, OUT, 7, 1.0, 2010.0, profit=1000)]
    clock.t += 60
    auto.check(feed)
    assert feed.history_calls == 2
    assert len(journals.stats(j["id"])["trades"]) == 1


def test_open_positions_refresh_now_and_then(journals):
    journals.create("Live", [], "51234567", auto_sync=True)
    feed, clock = FakeFeed(), Clock()
    feed.positions = [{"ticket": 9}]
    auto = AutoSync(journals, clock=clock)
    auto.check(feed)
    clock.t += 120
    auto.check(feed)
    assert feed.history_calls == 1
    clock.t += 300
    auto.check(feed)
    assert feed.history_calls == 2


def test_other_account_waits_without_touching_data(journals):
    journals.create("Live", [], "51234567", auto_sync=True)
    feed = FakeFeed(login="999")
    AutoSync(journals, clock=Clock()).check(feed)
    s = journals.list()[0]["sync"]
    assert feed.history_calls == 0
    assert s["state"] == "waiting" and "51234567" in s["error"]
    assert journals.list()[0]["synced_at"] is None


def test_switch_off_means_no_sync(journals):
    j = journals.create("Live", [], "51234567", auto_sync=True)
    journals.update(j["id"], {"auto_sync": False})
    feed = FakeFeed()
    AutoSync(journals, clock=Clock()).check(feed)
    assert feed.history_calls == 0


def test_failures_back_off(journals):
    journals.create("Live", [], "51234567", auto_sync=True)
    feed, clock = FakeFeed(), Clock()
    feed.fail = True
    auto = AutoSync(journals, clock=clock)
    auto.check(feed)
    assert journals.list()[0]["sync"]["state"] == "error"
    clock.t += 30
    auto.check(feed)
    assert feed.history_calls == 1  # still waiting out the first pause (60 s)
    clock.t += 31
    auto.check(feed)
    assert feed.history_calls == 2
    clock.t += 61
    auto.check(feed)
    assert feed.history_calls == 2  # the second pause is 120 s
    feed.fail = False
    clock.t += 60
    auto.check(feed)
    assert feed.history_calls == 3 and journals.list()[0]["sync"]["state"] == "ok"


def test_not_mt5_is_waiting(journals):
    journals.create("Live", [], "51234567", auto_sync=True)
    AutoSync(journals, clock=Clock()).check(object())
    s = journals.list()[0]["sync"]
    assert s["state"] == "waiting" and "MT5" in s["error"]
