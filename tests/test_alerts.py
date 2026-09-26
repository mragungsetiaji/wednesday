import io
import json
import urllib.error
import urllib.parse

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday import alerts as alerts_mod
from wednesday.alerts import AlertManager, AlertSettings, TelegramClient, TelegramError, format_alert
from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.levels import Level
from wednesday.scanner import LevelSet, ScanConfig, ScanResult, TimeframeResult
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME

T0 = pd.Timestamp("2026-03-02 10:00")


class FakeClient:
    def __init__(self, fail=False):
        self.token, self.chat_id = "t", "42"
        self.sent = []
        self.fail = fail

    def send(self, text):
        if self.fail:
            raise TelegramError("Can't reach Telegram: blocked")
        self.sent.append(text)


def m1(start, lows_highs):
    idx = pd.date_range(start, periods=len(lows_highs), freq="1min")
    lows, highs = zip(*lows_highs)
    return pd.DataFrame({"open": highs, "high": highs, "low": lows, "close": lows, "volume": 1.0}, index=idx)


def bull_ob(priority="extreme", confirmed=T0 - pd.Timedelta("1h")):
    return Level("ob", "bullish", 2471.0, 2469.5, confirmed - pd.Timedelta("20min"), confirmed, "BULL OB",
                 meta={"priority": priority, "entry": 2471.0, "sl": 2469.5, "risk": 1.5, "body": 1.5, "sl_capped": False})


def result(obs, tf="5M", price=2472.0):
    s = LevelSet(obs, None, None, [], [])
    return ScanResult(T0, price, ("ob",), [TimeframeResult(TIMEFRAMES_BY_NAME[tf], 100, {"ob": s})])


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'a.db'}")


def test_alerts_once_when_a_wick_enters_the_zone(store):
    client = FakeClient()
    mgr = AlertManager(store, client)
    ob = bull_ob()
    history = m1(T0, [(2473, 2475)] * 5)
    assert mgr.check("mt5", "XAUUSD", result([ob]), history) == []  # baseline only

    # Next minute stays above the zone: nothing.
    history = pd.concat([history, m1(T0 + pd.Timedelta("5min"), [(2472.5, 2474)])])
    assert mgr.check("mt5", "XAUUSD", result([ob]), history) == []

    # A wick to 2470.8 trades into 2469.5-2471.0 even though the close is higher.
    history = pd.concat([history, m1(T0 + pd.Timedelta("6min"), [(2470.8, 2473)])])
    sent = mgr.check("mt5", "XAUUSD", result([ob]), history)
    assert len(sent) == 1 and len(client.sent) == 1
    assert "entered 5M bullish OB" in client.sent[0] and "Buy limit <b>2,471.00</b>" in client.sent[0]

    # Still inside next minute: no repeat, also not after a restart (new manager, same store).
    history = pd.concat([history, m1(T0 + pd.Timedelta("7min"), [(2470.5, 2471.5)])])
    assert mgr.check("mt5", "XAUUSD", result([ob]), history) == []
    again = AlertManager(store, client)
    again.check("mt5", "XAUUSD", result([ob]), history.iloc[:-1])
    assert again.check("mt5", "XAUUSD", result([ob]), history) == []
    assert len(client.sent) == 1
    (row,) = store.recent_alerts()
    assert (row["timeframe"], row["status"], row["entry"]) == ("5M", "sent", 2471.0)


def test_bars_before_confirmation_do_not_alert():
    client = FakeClient()
    mgr = AlertManager(None, client)
    ob = bull_ob(confirmed=T0 + pd.Timedelta("5min"))  # 5M break candle opens at 10:05, closes 10:10
    history = m1(T0, [(2473, 2475)])
    mgr.check("mt5", "X", result([ob]), history)
    history = pd.concat([history, m1(T0 + pd.Timedelta("1min"), [(2470, 2472)] * 8)])  # 10:01..10:08
    assert mgr.check("mt5", "X", result([ob]), history) == []
    history = pd.concat([history, m1(T0 + pd.Timedelta("10min"), [(2470, 2472)])])  # 10:10
    assert len(mgr.check("mt5", "X", result([ob]), history)) == 1


def test_filters_by_timeframe_and_priority():
    client = FakeClient()
    mgr = AlertManager(None, client, AlertSettings(timeframes=["1H"], priorities=["extreme"]))
    history = m1(T0, [(2473, 2475)])
    for r in (result([bull_ob()], tf="5M"), result([bull_ob("middle")], tf="1H")):
        mgr._last_bar.clear()
        mgr.check("mt5", "X", r, history)
        assert mgr.check("mt5", "X", r, pd.concat([history, m1(T0 + pd.Timedelta("1min"), [(2470, 2472)])])) == []
    mgr.settings.enabled = False
    assert client.sent == []


def test_failed_send_is_logged_and_retried(store):
    client = FakeClient(fail=True)
    mgr = AlertManager(store, client)
    ob = bull_ob()
    history = m1(T0, [(2473, 2475)])
    mgr.check("mt5", "X", result([ob]), history)
    history = pd.concat([history, m1(T0 + pd.Timedelta("1min"), [(2470, 2472)])])
    assert mgr.check("mt5", "X", result([ob]), history) == []
    assert "blocked" in mgr.last_error
    assert store.recent_alerts()[0]["status"] == "failed"
    client.fail = False
    history = pd.concat([history, m1(T0 + pd.Timedelta("2min"), [(2470, 2472)])])
    assert len(mgr.check("mt5", "X", result([ob]), history)) == 1
    assert store.recent_alerts()[0]["status"] == "sent" and mgr.last_error is None


def test_format_alert_mentions_plan_and_structure():
    ob = bull_ob()
    ob.meta["sl_capped"] = True
    ob.touches = 2
    text = format_alert("XAU<USD>", "1H", ob, 2470.9, "bullish BOS at 2,452.86")
    assert "XAU&lt;USD&gt;" in text  # HTML-escaped
    assert "(extreme)" in text and "(capped)" in text and "Entry tested 2×" in text
    assert "1H structure: bullish BOS at 2,452.86" in text


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_telegram_client_send_and_chats(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, dict(urllib.parse.parse_qsl(req.data.decode()))))
        if req.full_url.endswith("/getUpdates"):
            payload = {"ok": True, "result": [{"message": {"chat": {"id": 7, "type": "private", "first_name": "Agung"}}}]}
        else:
            payload = {"ok": True, "result": {}}
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(alerts_mod.urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient("TOKEN", "7")
    client.send("hi")
    assert calls[0][0] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert calls[0][1]["chat_id"] == "7" and calls[0][1]["parse_mode"] == "HTML"
    assert client.chats() == [{"id": 7, "type": "private", "name": "Agung"}]


def test_telegram_client_reports_api_errors(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'))

    monkeypatch.setattr(alerts_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TelegramError, match="Unauthorized"):
        TelegramClient("bad", "1").send("x")
    with pytest.raises(TelegramError, match="TELEGRAM_CHAT_ID"):
        TelegramClient("t", None).send("x")


def test_alerts_api(store, tmp_path):
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["5M"],), params=DetectorParams(swing_length=2))
    client = FakeClient()
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, alerts=AlertManager(store, client))
    api = TestClient(create_app(runtime, ui_dir=tmp_path))

    body = api.get("/api/alerts").json()
    assert body["configured"] is True and body["settings"]["enabled"] is True
    assert api.put("/api/alerts", json={"timeframes": ["2H"]}).status_code == 422
    body = api.put("/api/alerts", json={"enabled": True, "timeframes": ["4H", "1H"], "priorities": ["extreme"]}).json()
    assert body["settings"]["timeframes"] == ["4H", "1H"]
    assert runtime.alerts.settings.priorities == ["extreme"]
    assert store.get_setting("alerts")["timeframes"] == ["4H", "1H"]

    assert api.post("/api/alerts/test").json() == {"ok": True}
    assert "Test message" in client.sent[-1]
    client.fail = True
    res = api.post("/api/alerts/test")
    assert res.status_code == 502 and "blocked" in res.json()["detail"]
