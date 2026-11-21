"""Reference data for the order block alerts (#96, step 9e): what ``alerts.py`` makes of given
order blocks, settings and M1 bars, for the Rust port (``server-rs/src/alerts/``) to match.

``tests/golden/alerts/`` holds:

- ``format.json``: ``format_alert`` texts for order blocks, prices, digits, structure and trade
  biases (thousands separators, HTML escaping, the RISK ON / OFF line);
- ``keys.json``: ``ob_key`` for order block times and tops (``{top:.5f}`` rounding);
- ``settings.json``: ``AlertSettings.from_dict`` + ``to_dict`` and ``validate`` for request bodies;
- ``check.json``: ``AlertManager.check`` scenarios: order blocks, settings and the M1 bars at each
  check, the trade bias, and the keys and messages each check sends.

``server-rs/tests/alerts_golden.rs`` runs the Rust code on the same input and wants the same
output. Rewrite the files after a deliberate change to the alerts, and review the diff:

    UPDATE_GOLDEN=1 uv run pytest tests/test_alerts_golden.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from wednesday.alerts import AlertManager, AlertSettings, TelegramError, format_alert, ob_key
from wednesday.bias import TradeBias
from wednesday.levels import Level
from wednesday.scanner import LevelSet, ScanResult, TimeframeResult
from wednesday.structure import Bias
from wednesday.timeframes import TIMEFRAMES_BY_NAME

GOLDEN = Path(__file__).with_name("golden") / "alerts"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"

T0 = pd.Timestamp("2026-03-02 10:00")


def ob(kind="bullish", top=2471.0, bottom=2469.5, *, confirmed=T0 - pd.Timedelta("1h"), priority="extreme",
       entry=None, sl=None, risk=None, sl_capped=False, swing=None, touches=0, age="20min") -> Level:
    entry = (top if kind == "bullish" else bottom) if entry is None else entry
    sl = (bottom if kind == "bullish" else top) if sl is None else sl
    meta = {"priority": priority, "entry": entry, "sl": sl, "risk": round(abs(entry - sl), 10) if risk is None else risk,
            "body": round(top - bottom, 10), "sl_capped": sl_capped, "swing": swing}
    tag = "BULL OB" if kind == "bullish" else "BEAR OB"
    return Level("ob", kind, top, bottom, confirmed - pd.Timedelta(age), confirmed,
                 tag if priority == "extreme" else f"{tag} (mid)", touches=touches, meta=meta)


def level_json(lv: Level) -> dict:
    return {"kind": lv.kind, "top": lv.top, "bottom": lv.bottom, "time": lv.time.isoformat(),
            "confirmed_time": lv.confirmed_time.isoformat(), "touches": lv.touches, "meta": lv.meta}


def bias_json(b: Bias | None) -> dict | None:
    return None if b is None else {"direction": b.direction, "event": b.event, "level": b.level}


def trade(direction: str | None) -> TradeBias | None:
    return TradeBias(direction) if direction else None


def structure(direction, event, level) -> Bias:
    return Bias(direction, event, level, T0, T0, 1, 1)


# ---- format_alert ----

FORMAT_CASES = [
    dict(symbol="XAUUSD", tf="5M", ob=ob(), price=2472.0),
    dict(symbol="XAUUSD", tf="1H", ob=ob("bearish", 2481.25, 2479.4, priority="middle", swing="LH", touches=2),
         price=2478.0, bias=structure("bearish", "CHoCH", 2490.5), trade="bearish"),
    dict(symbol="XAUUSD", tf="4H", ob=ob(top=2471.0, bottom=2455.0, entry=2471.0, sl=2461.0, risk=10.0, sl_capped=True,
                                         swing="HL"), price=2475.123, bias=structure("bullish", "BOS", 2433.333),
         trade="bearish"),
    dict(symbol="XAUUSD", tf="15M", ob=ob(touches=1), price=2470.0, trade="bullish"),
    dict(symbol="XAUUSD", tf="30M", ob=ob("bearish", 2481.0, 2480.0), price=2480.5, trade="neutral"),
    dict(symbol="BTC<USD>&'x\"", tf="5M", ob=ob(top=104999.995, bottom=104250.0), price=1234567.891,
         bias=structure("bullish", "BOS", 99999.999), digits=2),
    dict(symbol="EURUSD", tf="5M", ob=ob(top=1.08455, bottom=1.08412), price=1.084505, digits=5, trade="bullish"),
    dict(symbol="USDJPY", tf="1H", ob=ob("bearish", 151.234, 151.1), price=151.2, digits=3),
    dict(symbol="US30", tf="1H", ob=ob(top=39001.5, bottom=38990.0), price=39000.49, digits=0),
    dict(symbol="XAUUSD", tf="5M", ob=ob(top=999.995, bottom=999.0), price=999.9949999, trade="bullish"),
    dict(symbol="XAUUSD", tf="5M", ob=ob(priority="middle", swing=""), price=2470.0, bias=structure("bearish", "BOS", 0.125)),
]


def format_cases() -> list[dict]:
    out = []
    for c in FORMAT_CASES:
        b = c.get("bias")
        bias_text = f"{b.direction} {b.event} at {b.level:,.2f}" if b else None
        digits = c.get("digits", 2)
        out.append({"symbol": c["symbol"], "tf": c["tf"], "ob": level_json(c["ob"]), "price": c["price"],
                    "bias": bias_json(b), "digits": digits, "trade_bias": c.get("trade"),
                    "text": format_alert(c["symbol"], c["tf"], c["ob"], c["price"], bias_text, digits=digits,
                                         trade_bias=trade(c.get("trade")))})
    return out


# ---- ob_key ----

KEY_TOPS = [2471.0, 2471.123456789, 0.000012345, 1e6, 2471.000005, 1234.567895, 0.1 + 0.2, 1.000015, 2.675, 99999.999995]


def key_cases() -> list[dict]:
    out = []
    for i, top in enumerate(KEY_TOPS):
        lv = ob("bearish" if i % 2 else "bullish", top, top - 1, confirmed=T0 + pd.Timedelta(minutes=37 * i))
        out.append({"source": "tradingview", "symbol": "XAUUSD", "tf": ["5M", "1H", "4H"][i % 3], "ob": level_json(lv),
                    "key": ob_key("tradingview", "XAUUSD", ["5M", "1H", "4H"][i % 3], lv)})
    return out


# ---- AlertSettings ----

SETTINGS_BODIES = [
    {},
    {"enabled": False, "timeframes": ["1H", "4H"], "priorities": ["extreme"], "neutral_alerts": True, "chat_id": " 123 "},
    {"enabled": 0, "neutral_alerts": "yes", "chat_id": "-1001234567890"},
    {"enabled": "", "chat_id": "@my_channel"},
    {"chat_id": "my channel"},
    {"chat_id": "--12"},
    {"chat_id": "-"},
    {"chat_id": 123456789},
    {"chat_id": 0},
    {"chat_id": 12.5},
    {"chat_id": True},
    {"chat_id": None, "telegram_bot_token": "123:ABC", "forget_telegram_bot_token": True},
    {"timeframes": ["2H", "1H", 5], "priorities": ["low", None, "middle"]},
    {"timeframes": "4H"},
    {"timeframes": [], "priorities": []},
    {"timeframes": {"5M": 1}},
    {"timeframes": None},
    {"enabled": [], "neutral_alerts": [0], "chat_id": "  "},
    {"timeframes": ["it's"], "chat_id": "x\"y"},
]


def settings_cases() -> list[dict]:
    out = []
    for body in SETTINGS_BODIES:
        try:
            s = AlertSettings.from_dict(body)
        except Exception as exc:  # noqa: BLE001 - the route answers 500 here
            out.append({"body": body, "error": type(exc).__name__})
            continue
        out.append({"body": body, "settings": s.to_dict(), "errors": s.validate()})
    return out


# ---- AlertManager.check ----

class FakeClient:
    def __init__(self):
        self.token, self.chat_id = "t", "42"
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)


def bars(start: pd.Timestamp, rows: list[tuple[float, float]]) -> list[list]:
    """M1 bars from ``start``: (low, high) each."""
    return [[(start + pd.Timedelta(minutes=i)).isoformat(), hi, hi, lo, lo, 1.0] for i, (lo, hi) in enumerate(rows)]


def frame(rows: list[list]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    return pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close", "volume"], index=idx)


def scenario(name, settings, timeframes, steps, price=2472.0, source="tradingview", symbol="XAUUSD") -> dict:
    """``timeframes``: [(tf name, structure bias, [order blocks])]; ``steps``: [(new bars, trade bias)]."""
    client = FakeClient()
    mgr = AlertManager(None, client, AlertSettings.from_dict(settings))
    result = ScanResult(T0, price, ("ob",), [
        TimeframeResult(TIMEFRAMES_BY_NAME[tf], 100, {"ob": LevelSet(obs, None, None, [], [])}, bias)
        for tf, bias, obs in timeframes])
    history: list[list] = []
    out_steps = []
    for new, bias_dir in steps:
        history = history + new
        before = len(client.sent)
        keys = mgr.check(source, symbol, result, frame(history), trade(bias_dir))
        out_steps.append({"bars": len(history), "trade_bias": bias_dir, "keys": keys, "messages": client.sent[before:]})
    return {"name": name, "source": source, "symbol": symbol, "price": price, "settings": settings,
            "timeframes": [{"tf": tf, "bias": bias_json(b), "obs": [level_json(o) for o in obs]} for tf, b, obs in timeframes],
            "m1": history, "steps": out_steps}


def check_cases() -> list[dict]:
    bull5 = ob()  # 2469.5-2471.0, confirmed T0-1h
    bear5 = ob("bearish", 2481.0, 2480.0, swing="LH")
    mid5 = ob(top=2466.0, bottom=2465.0, priority="middle")
    late1h = ob(top=2474.0, bottom=2473.0, confirmed=T0 + pd.Timedelta("5min"))  # 1H: counts from T0+65min
    bull4h = ob(top=2471.5, bottom=2468.0, swing="HL", touches=1, confirmed=T0 - pd.Timedelta("5h"))
    s1 = scenario("enter-once", {}, [
        ("4H", structure("bullish", "BOS", 2433.5), [bull4h]),
        ("1H", None, [late1h]),
        ("5M", structure("bearish", "CHoCH", 2490.25), [bull5, bear5, mid5]),
    ], [
        (bars(T0, [(2473, 2475)] * 5), None),  # first check: only remembers
        (bars(T0 + pd.Timedelta("5min"), [(2472.5, 2474)]), None),  # touches the 1H OB before it counts
        (bars(T0 + pd.Timedelta("6min"), [(2470.8, 2473)]), None),  # wick into the 5M and 4H bullish OBs
        (bars(T0 + pd.Timedelta("7min"), [(2470.5, 2471.5)]), None),  # still inside: no repeat
        (bars(T0 + pd.Timedelta("8min"), [(2475, 2480.2), (2464.0, 2466.5)]), "bearish"),  # bear OB and the mid one
        ([], None),  # no new bars
        (bars(T0 + pd.Timedelta("65min"), [(2473.5, 2473.6)]), "bullish"),  # the 1H OB now counts
    ])
    s2 = scenario("filters", {"timeframes": ["4H", "1H"], "priorities": ["middle"], "chat_id": "7"}, [
        ("4H", None, [ob(top=2471.5, bottom=2468.0, priority="middle", sl_capped=True, entry=2471.5, sl=2466.5, risk=5.0,
                       confirmed=T0 - pd.Timedelta("4h"))]),
        ("1H", None, [ob(top=2470.0, bottom=2469.0)]),  # extreme: filtered out
        ("5M", None, [ob(top=2471.0, bottom=2470.0, priority="middle")]),  # 5M: filtered out
    ], [
        (bars(T0, [(2473, 2475)]), None),
        (bars(T0 + pd.Timedelta("1min"), [(2469.5, 2472)]), "bearish"),
    ])
    s3 = scenario("neutral-held-back", {}, [("5M", None, [bull5])], [
        (bars(T0, [(2473, 2475)]), "neutral"),
        (bars(T0 + pd.Timedelta("1min"), [(2470.0, 2472)]), "neutral"),  # held back; the bar is now seen
        (bars(T0 + pd.Timedelta("2min"), [(2472.0, 2473)]), "bullish"),  # outside: nothing
        (bars(T0 + pd.Timedelta("3min"), [(2470.9, 2473)]), "bullish"),
    ])
    s4 = scenario("neutral-alerts-on", {"neutral_alerts": True}, [("15M", None, [ob("bearish", 2481.0, 2480.0)])], [
        (bars(T0, [(2473, 2475)]), "neutral"),
        (bars(T0 + pd.Timedelta("1min"), [(2476.0, 2480.0)]), "neutral"),
    ], symbol="XAU<USD>")
    s5 = scenario("paused", {"enabled": False}, [("5M", None, [bull5])], [
        (bars(T0, [(2473, 2475)]), None),
        (bars(T0 + pd.Timedelta("1min"), [(2470.0, 2472)]), None),
    ])
    return [s1, s2, s3, s4, s5]


def _dump(v) -> str:
    return json.dumps(v, indent=1, ensure_ascii=False) + "\n"


FILES = {"format.json": format_cases, "keys.json": key_cases, "settings.json": settings_cases, "check.json": check_cases}


@pytest.mark.parametrize("name", sorted(FILES))
def test_alerts_reference_output_is_current(name):
    path = GOLDEN / name
    now = _dump(FILES[name]())
    if UPDATE or not path.exists():
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(now, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == now, (
        f"{path} is out of date with alerts.py; after a deliberate change run\n"
        "  UPDATE_GOLDEN=1 uv run pytest tests/test_alerts_golden.py\nand make the Rust port match.")


def test_the_scenarios_cover_what_they_say():
    cases = {c["name"]: c for c in check_cases()}
    sent = [len(s["keys"]) for s in cases["enter-once"]["steps"]]
    assert sent == [0, 0, 2, 0, 2, 0, 1], sent
    assert [len(s["keys"]) for s in cases["filters"]["steps"]] == [0, 1]
    assert [len(s["keys"]) for s in cases["neutral-held-back"]["steps"]] == [0, 0, 0, 1]
    assert [len(s["keys"]) for s in cases["neutral-alerts-on"]["steps"]] == [0, 1]
    assert [len(s["keys"]) for s in cases["paused"]["steps"]] == [0, 0]
    assert any(c.get("error") for c in settings_cases())
    assert any(c.get("errors") for c in settings_cases())


def test_a_failed_send_is_not_remembered():
    """What the Rust port's check does on a failed send, in Python: logged as failed, retried later."""
    class Failing(FakeClient):
        def send(self, text):
            raise TelegramError("Can't reach Telegram: blocked")

    mgr = AlertManager(None, Failing(), AlertSettings())
    result = ScanResult(T0, 2472.0, ("ob",), [TimeframeResult(TIMEFRAMES_BY_NAME["5M"], 100,
                                                              {"ob": LevelSet([ob()], None, None, [], [])})])
    history = bars(T0, [(2473, 2475)])
    mgr.check("s", "X", result, frame(history))
    history += bars(T0 + pd.Timedelta("1min"), [(2470, 2472)])
    assert mgr.check("s", "X", result, frame(history)) == []
    assert mgr.last_error == "Can't reach Telegram: blocked"
