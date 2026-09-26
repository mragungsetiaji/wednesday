from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday import brief as brief_mod
from wednesday.alerts import AlertManager, AlertSettings, format_alert
from wednesday.bias import TradeBias, active, expiry_time, setup_risk
from wednesday.brief import BriefError, BriefRunner, BriefSettings, build_input, html_to_text, suggested_bias
from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.scanner import LevelSet, ScanConfig, ScanResult, TimeframeResult
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME

from test_alerts import FakeClient, T0, bull_ob, m1
from test_orderblock import RALLY, candles, detect, mirror

UTC = timezone.utc


# ---- swing tags on order blocks -------------------------------------------

HIGHER_LOW = [(98, 99, 97, 98.5), (98.5, 99, 96, 97), (97, 98, 95, 96), (96, 98, 96, 97.5), (97.5, 99, 97, 98.5)]
LOWER_LOW = [(101, 101.5, 100.5, 101), (101, 101.5, 100, 100.2), (100.2, 101, 99.5, 100.5), (100.5, 101, 100, 100.8),
             (100.8, 101.5, 100.5, 101)]


def test_extreme_ob_is_tagged_higher_low_or_lower_low():
    ext = detect(candles(HIGHER_LOW + RALLY))[0]
    assert ext.meta["priority"] == "extreme" and ext.meta["swing"] == "HL"  # leg low 98 above the 95 swing low
    assert detect(candles(LOWER_LOW + RALLY))[0].meta["swing"] == "LL"
    assert detect(candles(RALLY))[0].meta["swing"] is None  # no earlier swing low to compare with
    mid = detect(candles(HIGHER_LOW + RALLY))[1]
    assert mid.meta["priority"] == "middle" and mid.meta["swing"] is None


def test_bearish_mirror_is_lower_high():
    ext = detect(mirror(candles(HIGHER_LOW + RALLY)))[0]
    assert ext.kind == "bearish" and ext.meta["swing"] == "LH"
    assert detect(mirror(candles(LOWER_LOW + RALLY)))[0].meta["swing"] == "HH"


# ---- bias and labels ----------------------------------------------------------

def test_expiry_is_the_new_york_close():
    wed = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)  # Wed 10:00 NY (EST)
    assert expiry_time("day", wed) == datetime(2026, 3, 4, 22, 0, tzinfo=UTC)  # 17:00 NY
    assert expiry_time("week", wed) == datetime(2026, 3, 6, 22, 0, tzinfo=UTC)  # Friday 17:00 NY
    fri_late = datetime(2026, 3, 6, 23, 0, tzinfo=UTC)  # Fri 18:00 NY: next close is Monday
    assert expiry_time("day", fri_late) == datetime(2026, 3, 9, 21, 0, tzinfo=UTC)  # EDT from 8 Mar
    assert expiry_time("none", wed) is None


def test_risk_labels_follow_the_bias():
    bear = TradeBias("bearish")
    assert (setup_risk("sell", bear), setup_risk("buy", bear)) == ("on", "off")
    assert (setup_risk("buy", TradeBias("bullish")), setup_risk("sell", TradeBias("neutral"))) == ("on", "no_trade")
    assert setup_risk("buy", None) is None
    old = TradeBias("bearish").stamped(datetime(2026, 3, 4, 15, 0, tzinfo=UTC))
    assert active(old, datetime(2026, 3, 4, 21, 59, tzinfo=UTC)) is old
    assert active(old, datetime(2026, 3, 4, 22, 0, tzinfo=UTC)) is None
    assert TradeBias("sideways").validate()


def sell_ob(price, swing, priority="extreme"):
    ob = bull_ob(priority)
    ob.kind, ob.top, ob.bottom = "bearish", price + 1, price
    ob.meta.update(entry=price, sl=price + 1, swing=swing)
    return ob


def test_with_bias_the_lower_high_ob_comes_first():
    near, lh = sell_ob(2475, "HH"), sell_ob(2490, "LH")
    s = LevelSet([near, lh], None, None, [], [])
    result = ScanResult(T0, 2470.0, ("ob",), [TimeframeResult(TIMEFRAMES_BY_NAME["1H"], 100, {"ob": s})])
    assert [lv.meta["swing"] for _, lv, _ in result.setups("sell")] == ["HH", "LH"]  # no bias: nearest first
    assert [lv.meta["swing"] for _, lv, _ in result.setups("sell", bias=TradeBias("bearish"))] == ["LH", "HH"]
    assert [lv.meta["swing"] for _, lv, _ in result.setups("sell", bias=TradeBias("bullish"))] == ["HH", "LH"]
    d = result.to_dict(TradeBias("bullish"))["setups"]["sell"]
    assert {s["risk"] for s in d} == {"off"}


def test_alerts_carry_the_label_and_hold_back_when_neutral():
    client = FakeClient()
    mgr = AlertManager(None, client)
    ob = bull_ob()
    history = m1(T0, [(2473, 2475)])
    s = LevelSet([ob], None, None, [], [])
    result = ScanResult(T0, 2472.0, ("ob",), [TimeframeResult(TIMEFRAMES_BY_NAME["5M"], 100, {"ob": s})])
    mgr.check("x", "X", result, history)
    entered = pd.concat([history, m1(T0 + pd.Timedelta("1min"), [(2470, 2472)])])
    assert mgr.check("x", "X", result, entered, TradeBias("neutral")) == []
    more = pd.concat([entered, m1(T0 + pd.Timedelta("2min"), [(2470, 2472)])])
    assert len(mgr.check("x", "X", result, more, TradeBias("bearish"))) == 1
    assert "RISK OFF</b> (against bearish bias)" in client.sent[0]
    assert AlertSettings.from_dict({"neutral_alerts": True}).neutral_alerts is True
    text = format_alert("XAUUSD", "1H", sell_ob(2490, "LH"), 2489.0, None, trade_bias=TradeBias("bearish"))
    assert "(extreme at LH)" in text and "RISK ON</b> (with bearish bias)" in text


# ---- brief ------------------------------------------------------------------

def test_html_to_text_and_bias_line():
    html = "<html><head><title>x</title><style>p{}</style></head><body><nav>menu</nav><p>CPI <b>hot</b></p>" \
           "<script>var a</script></body></html>"
    assert html_to_text(html) == "CPI hot"
    assert suggested_bias("- USD firm\nBIAS: Bearish") == "bearish"
    assert suggested_bias("no line") is None
    text = build_input({"Price": "2470"}, [{"url": "u", "ok": True, "truncated": True, "text": "news"},
                                           {"url": "v", "ok": False, "truncated": False, "text": ""}])
    assert '<source url="u" (cut to the first part of the page)>' in text and 'url="v"' not in text


def test_brief_settings_validation():
    assert BriefSettings.from_dict({"urls": ["https://a.com", " "], "model": " "}).urls == ["https://a.com"]
    assert BriefSettings.from_dict({"model": " "}).resolved_model == "claude-opus-5"
    assert BriefSettings(provider="openai").resolved_model == "gpt-5"
    assert BriefSettings(urls=["ftp://x"]).validate()
    assert BriefSettings(provider="other").validate()


def test_brief_generate_with_fakes(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(brief_mod, "fetch_source", lambda url, n: {
        "url": url, "ok": url.endswith("ok"), "error": None, "chars": 4, "truncated": False, "text": "news"})

    def fake_ask(model, system, user):
        calls.update(model=model, system=system, user=user)
        return "- DXY up\nBIAS: BEARISH"

    monkeypatch.setitem(brief_mod.ASK, "anthropic", fake_ask)
    store = Store(f"sqlite:///{tmp_path / 'b.db'}")
    runner = BriefRunner(store, BriefSettings(urls=["https://a/ok", "https://b/fail"], prompt="Be brief."))
    out = runner.generate({"Price": "2470"})
    assert out["suggested_bias"] == "bearish" and out["model"] == "claude-opus-5"
    assert [s["ok"] for s in out["sources"]] == [True, False] and "text" not in out["sources"][0]
    assert calls["system"] == "Be brief." and "Price: 2470" in calls["user"]
    runner.settings.urls = ["https://b/fail"]
    with pytest.raises(BriefError, match="None of the news URLs"):
        runner.generate({})


def test_brief_needs_key_and_urls(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(BriefError, match="ANTHROPIC_API_KEY"):
        BriefRunner(None, BriefSettings(urls=["https://a"])).check_ready()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(BriefError, match="news URL"):
        BriefRunner(None, BriefSettings()).check_ready()


# ---- API ---------------------------------------------------------------------

def test_bias_and_brief_api(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],), params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, brief=BriefRunner(store))
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    runtime.engine.feed.connect()
    runtime.engine.step()

    assert api.put("/api/bias", json={"direction": "up"}).status_code == 422
    body = api.put("/api/bias", json={"direction": "bearish", "note": "CPI hot", "expiry": "week"}).json()["bias"]
    assert body["direction"] == "bearish" and body["expires_at"] and body["expired"] is False
    assert TradeBias.from_dict(store.get_setting("bias")).note == "CPI hot"
    scan = api.get("/api/scan").json()
    assert scan["trade_bias"]["direction"] == "bearish"
    assert all(s["risk"] == "on" for s in scan["scan"]["setups"]["sell"])
    assert all(s["risk"] == "off" for s in scan["scan"]["setups"]["buy"])
    assert Runtime(cfg, DataSettings(source="synthetic"), store).bias.direction == "bearish"  # survives restarts
    assert api.put("/api/bias", json={"direction": None}).json() == {"bias": None}
    assert store.get_setting("bias") is None

    assert api.put("/api/brief", json={"urls": ["nope"]}).status_code == 422
    saved = api.put("/api/brief", json={"provider": "openai", "model": "gpt-x", "urls": ["https://a.com/x"]}).json()
    assert saved["settings"]["model"] == "gpt-x" and store.get_setting("brief")["provider"] == "openai"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = api.post("/api/brief/generate")
    assert res.status_code == 422 and "OPENAI_API_KEY" in res.json()["detail"]


def test_swings_are_labelled_against_the_previous_swing():
    from wednesday.structure import Context, label_swings

    ctx = Context(candles(HIGHER_LOW + RALLY))
    labels = [(p.kind, p.label) for p in label_swings(ctx.structure(2), ctx.times)]
    assert labels[0] == ("low", None)  # first swing low: nothing to compare with
    assert ("high", None) in labels
    lows = [p for p in label_swings(ctx.structure(2), ctx.times) if p.kind == "low"]
    assert [p.label for p in lows[1:]] == ["HL" if b.price > a.price else "LL" for a, b in zip(lows, lows[1:])]
    d = lows[0].to_dict()
    assert set(d) == {"kind", "time", "time_unix", "price", "label"}


def test_candles_endpoint_returns_swings(tmp_path):
    cfg = ScanConfig(lookback=100, timeframes=(TIMEFRAMES_BY_NAME["1H"],), params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), None)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    runtime.engine.feed.connect()
    runtime.engine.step()
    swings = api.get("/api/candles?tf=1H&limit=100").json()["swings"]
    assert swings and {s["label"] for s in swings} <= {"HH", "LH", "HL", "LL", None}
    assert {"HH", "LH"} & {s["label"] for s in swings} and {"HL", "LL"} & {s["label"] for s in swings}
