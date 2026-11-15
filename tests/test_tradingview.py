"""TradingView source: the Python side of the Node helper, with a Python stand-in for the helper.

The real helper (tradingview/feed.mjs) needs Node and the network, so these tests never start it.
"""

import sys
import textwrap

import pandas as pd
import pytest

from wednesday import feeds
from wednesday.feeds import TradingViewFeed, build_feed
from wednesday.settings import SOURCES, DataSettings, source_availability

# Speaks the helper's JSON-line protocol. M1 bars run up to and including the forming minute,
# like TradingView's; the "D" timeframe returns daily bars opening at 18:00 New York.
FAKE_HELPER = textwrap.dedent("""
    import json, sys, time
    now = int(time.time()) // 60 * 60
    for line in sys.stdin:
        req = json.loads(line)
        rid, op, sym = req["id"], req["op"], req.get("symbol")
        if sym != "FX:XAUUSD":
            out = {"id": rid, "error": {"code": "SYMBOL_ERROR", "message": "invalid symbol"}}
        elif op == "info":
            out = {"id": rid, "result": {"description": "XAU/USD Spot - Gold", "exchange": "FXCM"}}
        elif op == "candles" and req["timeframe"].isdigit():
            n, step = req["count"], int(req["timeframe"]) * 60
            last = (req["to"] if "to" in req else now) // step * step
            out = {"id": rid, "result": [[last - step * (n - 1 - i), 4000 + i, 4001 + i, 3999 + i, 4000.5 + i, 10]
                                          for i in range(n)]}
        elif op == "candles" and req["timeframe"] == "D":
            out = {"id": rid, "result": [[t, 1, 2, 0.5, 1.5, 100] for t in DAYS]}
        elif op == "quote":
            out = {"id": rid, "result": {"price": 4010.25, "time": now + 5}}
        elif op == "exit":
            sys.exit(0)
        else:
            out = {"id": rid, "error": {"code": "BAD_REQUEST", "message": "unknown op"}}
        print(json.dumps(out), flush=True)
""")


@pytest.fixture
def helper(tmp_path):
    def make(days=()):
        path = tmp_path / "fake_helper.py"
        path.write_text(f"DAYS = {list(days)!r}\n" + FAKE_HELPER)
        return [sys.executable, str(path)]
    return make


def test_fetch_m1_drops_the_forming_minute(helper):
    feed = TradingViewFeed("FX:XAUUSD", command=helper())
    try:
        feed.connect()
        bars = feed.fetch_m1(50)
    finally:
        feed.close()
    now = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("min")
    assert len(bars) == 50
    assert bars.index[-1] < now  # closed bars only
    assert bars.index.is_monotonic_increasing
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]


def test_unknown_symbol_says_what_to_use(helper):
    feed = TradingViewFeed("FXCM:XAUUSD", command=helper())
    try:
        with pytest.raises(RuntimeError, match="FX:XAUUSD"):
            feed.connect()
    finally:
        feed.close()


def test_last_tick_is_the_quote_in_utc(helper):
    feed = TradingViewFeed("FX:XAUUSD", command=helper())
    try:
        feed.connect()
        t, price = feed.last_tick()
    finally:
        feed.close()
    assert price == 4010.25
    assert t.tzinfo is None and t.second == 5


def test_helper_is_restarted_after_it_stops(helper):
    feed = TradingViewFeed("FX:XAUUSD", command=helper())
    try:
        feed.connect()
        with pytest.raises(RuntimeError, match="helper stopped"):
            feed._call("exit", 5)
        assert len(feed.fetch_m1(5)) == 5  # a fresh helper answers
    finally:
        feed.close()


def test_daily_history_is_indexed_by_new_york_trading_day(helper):
    # Bars open at 18:00 New York the evening before the day they close on (23:00 UTC in winter).
    opens = pd.to_datetime(["2026-11-08 23:00", "2026-11-09 23:00", "2026-11-10 23:00"]).astype("int64") // 10**9
    feed = TradingViewFeed("FX:XAUUSD", command=helper([int(t) for t in opens]))
    try:
        feed.connect()
        daily = feed.daily_history("UTC", 10)
    finally:
        feed.close()
    assert [d.strftime("%Y-%m-%d") for d in daily.index] == ["2026-11-09", "2026-11-10", "2026-11-11"]


def test_source_is_registered():
    info = SOURCES["tradingview"]
    assert info["default_symbol"] == "FX:XAUUSD" and info["default_clock"] == "UTC"
    settings = DataSettings(source="tradingview")
    assert settings.validate() == []
    feed = build_feed(settings)
    assert isinstance(feed, TradingViewFeed) and feed.symbol == "FX:XAUUSD"


def test_unavailable_without_the_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(feeds, "TRADINGVIEW_DIR", tmp_path)
    ok, why = source_availability("tradingview")
    assert not ok and "Node" in why
    (tmp_path / "feed.mjs").write_text("")
    monkeypatch.setattr(feeds.shutil, "which", lambda name: "/usr/bin/node")
    ok, why = source_availability("tradingview")
    assert not ok and "npm install --prefix tradingview" in why
    (tmp_path / "node_modules" / "@mathieuc" / "tradingview").mkdir(parents=True)
    assert source_availability("tradingview") == (True, None)


# ---- another symbol in a full-screen pane (markets.py, /api/markets/*) ----

from wednesday.markets import MarketError, Markets, clean_symbol  # noqa: E402
from wednesday.timeframes import TIMEFRAMES_BY_NAME  # noqa: E402


def test_pane_symbols_need_the_exchange_prefix():
    assert clean_symbol(" fx:xagusd ") == "FX:XAGUSD"
    for bad in ("XAGUSD", "", "FX:XAG USD", "../etc:passwd"):
        with pytest.raises(MarketError) as exc:
            clean_symbol(bad)
        assert exc.value.status == 400


def test_market_candles_on_the_feed_clock(helper):
    markets = Markets(command=lambda: (helper(), None))
    try:
        h1 = TIMEFRAMES_BY_NAME["1H"]
        utc = markets.candles("FX:XAUUSD", h1, 20, "UTC")
        ny7 = markets.candles("FX:XAUUSD", h1, 20, "NY+7")
        assert markets.precision("FX:XAUUSD") == 2  # no pricescale: gold's two decimals
    finally:
        markets.close()
    assert len(utc) == 20 and list(utc.columns) == ["open", "high", "low", "close", "volume"]
    assert (utc.index[1:] - utc.index[:-1] == pd.Timedelta(hours=1)).all()
    # NY+7 is New York wall time plus 7 h: UTC+3 in New York summer, UTC+2 in winter.
    shift = ny7.index[-1] - utc.index[-1]
    assert shift in (pd.Timedelta(hours=2), pd.Timedelta(hours=3))


def test_market_candles_page_back_before_a_time(helper):
    markets = Markets(command=lambda: (helper(), None))
    try:
        h1 = TIMEFRAMES_BY_NAME["1H"]
        latest = markets.candles("FX:XAUUSD", h1, 10, "UTC")
        first = int(latest.index[0].timestamp())
        older = markets.candles("FX:XAUUSD", h1, 10, "UTC", before=first)
    finally:
        markets.close()
    assert len(older) > 0 and older.index[-1] < latest.index[0]


def test_market_errors_carry_a_status(helper):
    markets = Markets(command=lambda: (helper(), None))
    try:
        with pytest.raises(MarketError) as exc:
            markets.tick("FX:NOPE", "UTC")
        assert exc.value.status == 404 and "FX:XAGUSD" in str(exc.value)
    finally:
        markets.close()
    missing = Markets(command=lambda: (None, "Node.js isn't installed."))
    with pytest.raises(MarketError) as exc:
        missing.tick("FX:XAUUSD", "UTC")
    assert exc.value.status == 503 and "Node.js" in str(exc.value)


def test_market_endpoints(helper, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from wednesday import server
    from wednesday.engine import Engine
    from wednesday.feeds import SyntheticFeed
    from wednesday.scanner import ScanConfig

    made = []
    monkeypatch.setattr(server, "Markets", lambda: made.append(Markets(command=lambda: (helper(), None))) or made[-1])
    cfg = ScanConfig(lookback=100)
    engine = Engine(SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100), cfg, "XAUUSD")
    client = TestClient(server.create_app(engine, source="synthetic", ui_dir=tmp_path))
    try:
        body = client.get("/api/markets/candles?symbol=fx:xauusd&tf=15M&limit=30").json()
        assert body["symbol"] == "FX:XAUUSD" and body["timeframe"] == "15M" and body["precision"] == 2
        assert len(body["candles"]) == 30 and body["levels"] == [] and body["has_more"]
        tick = client.get("/api/markets/tick?symbol=FX:XAUUSD").json()
        assert tick["price"] == 4010.25 and isinstance(tick["time"], int)
        assert client.get("/api/markets/tick?symbol=XAGUSD").status_code == 400
        assert client.get("/api/markets/candles?symbol=FX:NOPE").status_code == 404
        assert client.get("/api/markets/candles?symbol=FX:XAUUSD&tf=7M").status_code == 404
    finally:
        for m in made:
            m.close()
