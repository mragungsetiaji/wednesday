import threading

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday import server
from wednesday.engine import Runtime
from wednesday.journal.imports import BALANCE, BUY, IN, INOUT, OUT, SELL, ReportError, from_deals, parse_report
from wednesday.journal.service import Journals, canonical
from wednesday.journal.stats import analyse
from wednesday.plugins import PluginInfo
from wednesday.scanner import ScanConfig
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME

T0 = int(pd.Timestamp("2026-03-02 00:00").timestamp())


def deal(ticket, t, kind, entry=IN, position=0, volume=0.0, price=0.0, profit=0.0, commission=0.0, swap=0.0):
    return {"ticket": ticket, "time": t, "time_msc": t * 1000, "type": kind, "entry": entry, "position_id": position,
            "symbol": "XAUUSD" if kind in (BUY, SELL) else "", "volume": volume, "price": price, "profit": profit,
            "commission": commission, "fee": 0.0, "swap": swap, "comment": ""}


def test_deals_become_trades_with_partial_closes():
    deals = [
        deal(1, T0, BALANCE, profit=10_000),
        deal(2, T0 + 60, BUY, IN, 7, 1.0, 2000.0, commission=-7),
        deal(3, T0 + 600, SELL, OUT, 7, 0.4, 2010.0, profit=400),
        deal(4, T0 + 900, SELL, OUT, 7, 0.6, 2005.0, profit=300),
        deal(5, T0 + 1000, SELL, IN, 8, 0.5, 2004.0),
        deal(6, T0 + 1200, BUY, INOUT, 8, 1.0, 2002.0, profit=100),  # closes the sell, opens a 0.5 buy
        deal(7, T0 + 2000, BALANCE, profit=-500),
    ]
    trades, cash = from_deals(deals, positions=[{"ticket": 8, "price_current": 2003.0, "profit": 50.0, "swap": 0.0,
                                                 "sl": 0.0, "tp": 0.0}])
    assert [c["kind"] for c in cash] == ["deposit", "withdrawal"]
    by = {t["id"]: t for t in trades}
    a, b = by["p7:3"], by["p7:4"]
    assert (a["volume"], b["volume"]) == (0.4, 0.6)
    assert a["open_price"] == b["open_price"] == 2000.0
    assert a["commission"] == pytest.approx(-2.8) and b["commission"] == pytest.approx(-4.2)
    rev = by["p8:6"]
    assert rev["side"] == "sell" and rev["volume"] == 0.5 and rev["profit"] == 100
    still = by["p8"]  # the reversed half, still open
    assert still["side"] == "buy" and still["close_time"] is None and still["volume"] == 0.5
    assert still["open_price"] == 2002.0 and still["close_price"] == 2003.0


REPORT = """<html><body><table>
<tr><th colspan="14"><div><b>Trade History Report</b></div></th></tr>
<tr><th colspan="3">Name:</th><th colspan="11"><b>Someone</b></th></tr>
<tr><th colspan="3">Account:</th><th colspan="11"><b>51234567 (USD, Broker-Live, real, Hedge)</b></th></tr>
<tr><th colspan="3">Company:</th><th colspan="11"><b>Broker Ltd</b></th></tr>
<tr><th colspan="14"><div><b>Positions</b></div></th></tr>
<tr><td>Time</td><td>Position</td><td>Symbol</td><td>Type</td><td class="hidden" colspan="8"></td><td>Volume</td><td>Price</td>
<td>S / L</td><td>T / P</td><td>Time</td><td>Price</td><td>Commission</td><td>Swap</td><td>Profit</td></tr>
<tr><td>2026.03.02 00:01:00</td><td>7</td><td>XAUUSD.m</td><td>buy</td><td class="hidden" colspan="8"></td><td>1</td><td>2 000.00</td>
<td></td><td>2 020.00</td><td>2026.03.02 00:10:00</td><td>2 010.00</td><td>-7.00</td><td>0.00</td><td>1 000.00</td></tr>
<tr><td colspan="10"></td><td>-7.00</td><td>0.00</td><td>1 000.00</td></tr>
<tr><th colspan="14"><div><b>Orders</b></div></th></tr>
<tr><td>Open Time</td><td>Order</td><td>Symbol</td><td>Type</td></tr>
<tr><td>2026.03.02 00:01:00</td><td>70</td><td>XAUUSD.m</td><td>buy</td></tr>
<tr><th colspan="14"><div><b>Deals</b></div></th></tr>
<tr><td>Time</td><td>Deal</td><td>Symbol</td><td>Type</td><td>Direction</td><td>Volume</td><td>Price</td><td>Order</td>
<td>Commission</td><td>Fee</td><td>Swap</td><td>Profit</td><td>Balance</td><td>Comment</td></tr>
<tr><td>2026.03.01 20:00:00</td><td>1</td><td></td><td>balance</td><td></td><td></td><td></td><td></td>
<td>0.00</td><td>0.00</td><td>0.00</td><td>10 000.00</td><td>10 000.00</td><td>Deposit</td></tr>
<tr><td>2026.03.02 00:01:00</td><td>2</td><td>XAUUSD.m</td><td>buy</td><td>in</td><td>1</td><td>2000</td><td>70</td>
<td>-7.00</td><td>0.00</td><td>0.00</td><td>0.00</td><td>9 993.00</td><td></td></tr>
<tr><th colspan="14"><div><b>Results</b></div></th></tr>
</table></body></html>"""


def test_parse_mt5_report():
    got = parse_report(REPORT.encode("utf-16"))
    assert got["account"] == {"name": "Someone", "login": "51234567", "currency": "USD", "server": "Broker-Live",
                              "company": "Broker Ltd"}
    (t,) = got["trades"]
    assert (t["id"], t["symbol"], t["side"], t["volume"]) == ("p7", "XAUUSD.m", "buy", 1.0)
    assert (t["open_price"], t["close_price"], t["profit"], t["tp"]) == (2000.0, 2010.0, 1000.0, 2020.0)
    assert t["close_time"] - t["open_time"] == 540
    assert got["cash"] == [{"id": "1", "time": T0 - 4 * 3600, "kind": "deposit", "amount": 10_000.0, "comment": "Deposit"}]
    with pytest.raises(ReportError):
        parse_report(b"just text")


def test_canonical_symbols():
    assert {canonical(s) for s in ("XAUUSD", "XAUUSD.m", "XAUUSDm", "XAUUSD#", "GOLD", "gold.r")} == {"XAUUSD"}
    assert canonical("EURUSD.pro") == "EURUSD"


# ---- stats ---------------------------------------------------------------------------------

def price_path(points, start=T0):
    """M1 bars through (minute, price) points, linear in between, 0.5 wide."""
    s = pd.Series({m: p for m, p in points}).reindex(range(points[-1][0] + 1)).interpolate()
    idx = pd.to_datetime([start + 60 * m for m in s.index], unit="s")
    return pd.DataFrame({"open": s.values, "high": s.values + 0.25, "low": s.values - 0.25, "close": s.values,
                         "volume": 1.0}, index=idx)


def trade(i, open_m, close_m, open_p, close_p, side="buy", volume=1.0, mult=100.0, symbol="XAUUSD"):
    sign = 1 if side == "buy" else -1
    return {"id": f"t{i}", "position": str(i), "symbol": symbol, "side": side, "volume": volume,
            "open_time": T0 + 60 * open_m, "open_price": open_p,
            "close_time": T0 + 60 * close_m if close_m is not None else None, "close_price": close_p,
            "profit": sign * (close_p - open_p) * volume * mult if close_p else 0.0, "commission": 0.0, "swap": 0.0,
            "sl": None, "tp": None, "comment": None}


def test_drawdown_comes_from_the_price_not_the_closed_result():
    bars = {"XAUUSD": price_path([(0, 2000), (30, 1950), (60, 2010), (90, 2010)])}  # dips 50 while the buy is open
    cash = [{"id": "d", "time": T0 - 60, "kind": "deposit", "amount": 10_000.0, "comment": None}]
    trades = [trade(1, 0, 60, 2000.0, 2010.0)]  # closed +1000: a statement shows no drawdown at all
    got = analyse(trades, cash, bars, offset=0)
    s = got["summary"]
    assert s["gain"] == 10.0 and s["abs_gain"] == 10.0 and s["balance"] == 11_000
    # floating low: (1949.75 - 2000) * 100 = -5025 on 10 000
    assert s["drawdown"] == pytest.approx(50.25, abs=0.01)
    assert s["max_floating_loss"] == pytest.approx(-5025)
    t = got["trades"][0]
    assert t["verified"] and t["price_ok"] and t["mae"] == pytest.approx(-5025)
    assert got["verification"]["basis"] == "ohlc" and got["verification"]["coverage"] == 100.0

    blind = analyse(trades, cash, {}, offset=0)  # no prices: closed results only, and it says so
    assert blind["summary"]["drawdown"] == 0.0 and blind["verification"]["basis"] == "closed"
    assert blind["verification"]["no_prices"] == ["XAUUSD"]


def test_gain_is_time_weighted():
    bars = {"XAUUSD": price_path([(0, 2000), (400, 2000)])}
    cash = [{"id": "d1", "time": T0 - 60, "kind": "deposit", "amount": 1000.0, "comment": None},
            {"id": "d2", "time": T0 + 150 * 60, "kind": "deposit", "amount": 9000.0, "comment": None},
            {"id": "w", "time": T0 + 350 * 60, "kind": "withdrawal", "amount": -5000.0, "comment": None}]
    t1 = {**trade(1, 10, 100, 2000.0, 2000.0), "profit": 100.0}  # +10% on 1000
    t2 = {**trade(2, 200, 300, 2000.0, 2000.0), "profit": 1010.0}  # +10% on 10 100 (1100 + the 9000 deposit)
    got = analyse([t1, t2], cash, bars, offset=0)["summary"]
    assert got["gain"] == pytest.approx(21.0)  # 1.1 * 1.1: the deposit doesn't count as growth
    assert got["abs_gain"] == pytest.approx(11.1)  # 1110 profit on 10 000 deposited
    assert (got["deposits"], got["withdrawals"], got["balance"]) == (10_000, 5000, 6110)


def test_clock_offset_is_found_and_bad_prices_are_flagged():
    bars = {"XAUUSD.m": price_path([(0, 2000), (600, 2060)])}  # +0.1 a minute
    shift = 3 * 3600  # broker clock 3h ahead of the price feed
    trades = [trade(i, 20 * i, 20 * i + 10, 2000 + 2 * i, 2000 + 2 * i + 1, symbol="XAUUSD.m") for i in range(1, 20)]
    for t in trades:
        t["open_time"] += shift
        t["close_time"] += shift
    fake = trade(99, 500, 510, 1900.0, 1990.0, symbol="XAUUSD.m")  # prices the market never had
    fake["open_time"] += shift
    fake["close_time"] += shift
    cash = [{"id": "d", "time": T0 + shift - 60, "kind": "deposit", "amount": 10_000.0, "comment": None}]
    got = analyse(trades + [fake], cash, bars)
    v = got["verification"]
    assert v["offset_hours"] == 3 and v["offset_detected"]
    assert v["mismatched"] == ["t99"] and v["verified"] == 19
    assert got["trades"][-1]["verified"] is False


def test_open_trade_floats_at_the_last_price():
    bars = {"XAUUSD": price_path([(0, 2000), (100, 1990)])}
    cash = [{"id": "d", "time": T0 - 60, "kind": "deposit", "amount": 10_000.0, "comment": None}]
    t = trade(1, 0, None, 2000.0, None)
    got = analyse([t, {**trade(2, 0, 50, 2000.0, 1995.0)}], cash, bars, offset=0)
    s = got["summary"]
    assert s["floating"] == pytest.approx(-1000)  # (1990 - 2000) * 1 lot * 100 learnt from the closed trade
    assert s["equity"] == pytest.approx(s["balance"] - 1000)
    assert got["trading"]["open"] == 1


# ---- engine thread ------------------------------------------------------------------------

def test_engine_call_runs_on_the_scan_thread(tmp_path):
    runtime = Runtime(ScanConfig(lookback=40, timeframes=(TIMEFRAMES_BY_NAME["5M"],)), DataSettings(source="synthetic"))
    engine = runtime.engine
    with pytest.raises(RuntimeError):
        engine.call(lambda feed: 1)
    engine._thread = threading.Thread(target=engine._idle, args=(30,), daemon=True)
    engine._thread.start()
    try:
        assert engine.call(lambda feed: (threading.current_thread() is engine._thread, feed.name)) == (True, "synthetic")
        with pytest.raises(ZeroDivisionError):
            engine.call(lambda feed: 1 / 0)
    finally:
        engine._stop.set()
        engine._thread.join(2)


# ---- API ----------------------------------------------------------------------------------

@pytest.fixture
def journal_api(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 'j.db'}")
    store.save_bars("mt5", "XAUUSD", price_path([(0, 1995), (61, 2000), (70, 2010), (600, 2010)], start=T0 - 3600))
    runtime = Runtime(ScanConfig(lookback=40, timeframes=(TIMEFRAMES_BY_NAME["5M"],)), DataSettings(source="synthetic"),
                      store, journals=Journals(store))
    enabled: list[str] = []
    monkeypatch.setattr(server, "load_plugins",
                        lambda app, rt: [PluginInfo("fake", None, 1, enabled, loaded=True)])
    return TestClient(server.create_app(runtime, ui_dir=tmp_path)), enabled


def test_journal_api_flow(journal_api):
    api, enabled = journal_api
    assert api.get("/api/journals").json() == {"available": True, "journals": [], "multi": False}
    j = api.post("/api/journals", json={"name": "Main"}).json()
    r = api.post("/api/journals", json={"name": "Second"})
    assert r.status_code == 402 and "One journal is free" in r.json()["detail"]
    enabled.append("journal.multi")
    assert api.post("/api/journals", json={"name": "Second"}).status_code == 200
    assert api.get("/api/journals").json()["multi"] is True

    assert api.post(f"/api/journals/{j['id']}/import", content=b"nope").status_code == 422
    got = api.post(f"/api/journals/{j['id']}/import", content=REPORT.encode("utf-16")).json()
    assert got["trades"] == 1 and got["journal"]["login"] == "51234567"
    st = api.get(f"/api/journals/{j['id']}").json()
    assert st["summary"]["balance"] == 10_993 and st["summary"]["gain"] == pytest.approx(9.93)
    v = st["verification"]
    assert v["prices_from"] == {"XAUUSD.m": "mt5:XAUUSD"} and v["verified"] == 1 and v["offset_hours"] == 0
    assert st["trades"][0]["mae"] <= 0

    tid = st["trades"][0]["id"]
    api.put(f"/api/journals/{j['id']}/trades/{tid}/note", json={"note": "waited for the sweep", "tags": ["A+ setup", "a+ setup"]})
    st = api.get(f"/api/journals/{j['id']}").json()
    assert st["trades"][0]["note"] == "waited for the sweep" and st["trades"][0]["tags"] == ["a+ setup"]
    csv = api.get(f"/api/journals/{j['id']}/trades.csv").text
    assert "waited for the sweep" in csv.splitlines()[1]

    r = api.patch(f"/api/journals/{j['id']}", json={"time_offset": 2, "name": "Live"})
    assert r.json()["time_offset"] == 2 and r.json()["name"] == "Live"
    assert api.get(f"/api/journals/{j['id']}").json()["verification"]["offset_detected"] is False
    assert api.patch(f"/api/journals/{j['id']}", json={"time_offset": 30}).status_code == 422

    r = api.post(f"/api/journals/{j['id']}/sync")
    assert r.status_code == 409 and "isn't MT5" in r.json()["detail"]
    t = api.get("/api/journals/terminal").json()
    assert t["connected"] is False and "isn't MT5" in t["detail"]
    assert api.delete(f"/api/journals/{j['id']}").json() == {"deleted": True}
    assert api.get(f"/api/journals/{j['id']}").status_code == 404


def test_sync_refuses_another_account(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'j.db'}")
    js = Journals(store)
    j = js.create("Main", [])
    history = {"account": {"login": "1", "balance": 100.0, "equity": 100.0, "currency": "USD"},
               "deals": [deal(1, T0, BALANCE, profit=100)], "positions": []}
    assert js.sync(j["id"], history)["cash"] == 1
    st = js.stats(j["id"])
    assert st["verification"]["balance_matches"] is True
    with pytest.raises(ValueError, match="account 1"):
        js.sync(j["id"], {**history, "account": {"login": "2"}})


def test_create_ties_the_journal_to_its_account(tmp_path):
    js = Journals(Store(f"sqlite:///{tmp_path / 'j.db'}"))
    main = js.create("Main", ["journal.multi"], login=" 51234567 ")
    assert main["login"] == "51234567"
    with pytest.raises(ValueError, match="Main already follows account 51234567"):
        js.create("Again", ["journal.multi"], login="51234567")
    with pytest.raises(ValueError, match="digits"):
        js.create("Typo", ["journal.multi"], login="5123abc")
    prop = js.create("Prop", ["journal.multi"], login="777")
    history = {"account": {"login": "51234567"}, "deals": [], "positions": []}
    with pytest.raises(ValueError, match="logged in to account 51234567, but this journal follows account 777"):
        js.sync(prop["id"], history)
    assert js.sync(main["id"], history)["journal"]["login"] == "51234567"
    assert len(js.list()) == 2


def test_a_close_while_another_trade_floats_is_not_a_new_high():
    bars = {"XAUUSD": price_path([(0, 2000), (30, 1950), (60, 1950), (120, 2000), (130, 2000)])}
    cash = [{"id": "d", "time": T0 - 60, "kind": "deposit", "amount": 10_000.0, "comment": None}]
    stuck = trade(1, 0, 120, 2000.0, 2000.0)  # sits 50 under water, closes flat
    hedge = trade(2, 10, 30, 1983.0, 1950.0, side="sell")  # banks +3300 while the other is -5000
    got = analyse([stuck, hedge], cash, bars, offset=0)["summary"]
    # equity never went above 10 000 before the low (about 10 000 + 3300 - 5025), so the fall is ~17%, not ~38%
    assert 15 < got["drawdown"] < 20


def test_monthly_gain_daily_pnl_and_pips():
    bars = {"XAUUSD": price_path([(0, 2000), (10, 2000)])}
    cash = [{"id": "d", "time": T0 - 60, "kind": "deposit", "amount": 10_000.0, "comment": None}]
    day = 86400
    a = {**trade(1, 0, 5, 2000.0, 2010.0), "close_time": T0 + 5 * day}  # +1000, +100 pips, March
    b = {**trade(2, 0, 5, 2000.0, 1995.0), "close_time": T0 + 5 * day + 60}  # -500, -50 pips, same day
    c = {**trade(3, 0, 5, 2000.0, 2011.0, side="sell"), "close_time": T0 + 40 * day}  # -1100, April
    got = analyse([a, b, c], cash, bars, offset=0)
    assert [(m["month"], m["gain"], m["profit"], m["pips"]) for m in got["monthly"]] == [
        ("2026-03", 5.0, 500.0, 50.0), ("2026-04", -10.48, -1100.0, -110.0)]
    assert got["daily"][0] == {"day": "2026-03-07", "profit": 500.0, "pips": 50.0, "trades": 2, "won": 1}
    assert got["daily"][1]["day"] == "2026-04-11"
