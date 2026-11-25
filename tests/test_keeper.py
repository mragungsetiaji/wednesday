"""Markets kept live: stored beside the scanned one, dropped when no longer kept."""

import pandas as pd

from wednesday.feeds import SyntheticFeed
from wednesday.keeper import Keeper
from wednesday.storage import Store


def test_kept_markets_are_stored_and_let_go(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    rows = store.markets()
    for m in rows:
        m["keep_live"] = m["symbol"] in ("FX:XAUUSD", "FX:XAGUSD")
    store.save_markets(rows)
    scanned = ["tradingview", "FX:XAUUSD"]
    made = []

    def feed(symbol):
        made.append(symbol)
        return SyntheticFeed(history=6000, end=pd.Timestamp("2026-03-02 12:00"))

    keeper = Keeper(store, lambda: tuple(scanned), feed)
    keeper.round()
    assert made == ["FX:XAGUSD"]  # the scanned one is the engine's
    first = store.last_bar("tradingview", "FX:XAGUSD")
    assert first is not None
    keeper.round()
    assert store.last_bar("tradingview", "FX:XAGUSD")[0] > first[0]  # each round adds the new minute

    scanned[1] = "FX:XAGUSD"  # the trader switched to silver: the engine takes it over
    keeper.round()
    assert keeper.kept.keys() == {"FX:XAUUSD"}

    for m in rows:
        m["keep_live"] = False
    store.save_markets(rows)
    keeper.round()
    assert keeper.kept == {}
    assert store.last_bar("tradingview", "FX:XAUUSD") is not None


def test_nothing_is_kept_on_synthetic(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    rows = store.markets()
    rows[1]["keep_live"] = True
    store.save_markets(rows)
    keeper = Keeper(store, lambda: ("synthetic", "XAUUSD"), lambda s: SyntheticFeed())
    keeper.round()
    assert keeper.kept == {}
