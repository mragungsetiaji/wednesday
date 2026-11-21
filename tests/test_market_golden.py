"""Reference data for the bias a ``PUT /api/bias`` saves and the market a journal entry freezes (#96,
step 9f): ``tests/golden/market/frozen.json``. The Rust port (``wed-server/tests/it/market_golden.rs``)
must give the same output.

* ``bias``: a request body and the time it is made in, out the bias saved (``TradeBias(...).stamped()``,
  as ``put_bias`` in server.py builds it) or the 422 message; across New York's daylight saving
  changes, weekends, Fridays and the 17:00 close.
* ``market``: ``journal.market.freeze`` of the scan of a golden input (``tests/golden/<case>/m1.csv.gz``)
  with a bias, the upcoming news, the live price and the time. The scans given to ``freeze`` are in
  ``scans``, so Rust checks ``freeze`` alone exactly, and again on its own scan.

    UPDATE_GOLDEN=1 uv run pytest tests/test_market_golden.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from test_golden import CASES, _clean, read_input

from wednesday.bias import TradeBias
from wednesday.detectors import DetectorParams
from wednesday.journal.market import freeze
from wednesday.scanner import ScanConfig, scan

PATH = Path(__file__).with_name("golden") / "market" / "frozen.json"

NOWS = [
    "2026-03-04T15:00:00+00:00",  # Wednesday morning in New York (EST)
    "2026-03-04T22:00:00+00:00",  # 17:00 NY exactly: the next close
    "2026-03-04T21:59:59.999999+00:00",
    "2026-03-06T21:30:00.123456+00:00",  # Friday before the close
    "2026-03-06T23:00:00+00:00",  # Friday after the close: Monday's
    "2026-03-07T12:00:00+00:00",  # Saturday
    "2026-03-08T06:30:00+00:00",  # Sunday, the night clocks go forward
    "2026-03-08T22:30:00+00:00",  # Sunday evening, EDT
    "2026-10-30T20:00:00+00:00",  # Friday, EDT
    "2026-11-01T05:30:00+00:00",  # Sunday, inside the repeated hour
    "2026-11-01T22:30:00+00:00",  # Sunday evening, EST again
    "2026-12-31T23:59:00+00:00",  # Thursday, over the year end
]

BODIES = [
    {"direction": "bearish", "note": "CPI hot", "expiry": "day"},
    {"direction": "bullish", "note": "  DXY weak \n", "expiry": "week"},
    {"direction": "neutral", "expiry": "none"},
    {"direction": "bullish", "note": None, "expiry": None},
    {"direction": "bullish", "note": "é" * 500, "expiry": ""},
    {"direction": "bullish", "note": "é" * 501},
    {"direction": "up"},
    {"direction": "it's", "expiry": "month"},
    {"direction": 5, "expiry": 0, "note": 0},
    {"direction": True, "expiry": "week", "note": "x" * 600},
    {"direction": "Bearish"},
    {"direction": None},
    {"direction": ""},
    {"direction": 0},
    {"note": "no direction"},
]


def put_bias(body: dict, now: datetime) -> dict:
    """What ``PUT /api/bias`` saves (or refuses) for ``body`` at ``now``."""
    if not body.get("direction"):
        return {"saved": None, "error": None}
    new = TradeBias(str(body["direction"]), str(body.get("note") or ""), str(body.get("expiry") or "day"))
    if errors := new.validate():
        return {"saved": None, "error": "; ".join(errors)}
    return {"saved": new.stamped(now).to_dict(now), "error": None}


def bias_cases() -> list[dict]:
    out = []
    for body in BODIES:
        for now in NOWS if body.get("direction") in ("bearish", "bullish", "neutral") else NOWS[:1]:
            out.append({"now": now, "body": body, **put_bias(body, datetime.fromisoformat(now))})
    return out


NEWS = [
    {"time": "2026-03-04T15:30:00+00:00", "currency": "USD", "title": "ISM Services PMI", "impact": "high",
     "forecast": "52.0"},
    {"time": "2026-03-04T13:15:00+00:00", "currency": "USD", "title": "ADP Non-Farm Employment Change", "impact": "high"},
    {"time": "2026-03-06T13:30:00+00:00", "currency": "USD", "title": "Non-Farm Employment Change", "impact": "high"},
    {"time": "2026-03-04T15:30:00+00:00", "currency": "USD", "title": "Crude Oil Inventories", "impact": "medium"},
    {"time": "2026-03-05T13:30:00+00:00", "title": "Unemployment Claims"},
    {"time": "2026-03-04T15:00:00+00:00", "currency": "EUR", "title": "At the minute", "impact": "high"},
]

BEARISH = {"direction": "bearish", "note": "CPI hot", "expiry": "week", "set_at": "2026-03-04T14:00:00+00:00",
           "expires_at": "2026-03-06T22:00:00+00:00", "expired": False}
NEUTRAL = {"direction": "neutral", "note": "", "expiry": "none", "set_at": "2026-03-04T14:00:00.250000+00:00",
           "expires_at": None, "expired": False}

# (name, golden input, bias, news, price, now)
MARKETS = [
    ("no-bias", "utc-default", None, NEWS, 2164.5, "2026-03-04T15:00:00+00:00"),
    ("bearish", "utc-default", BEARISH, NEWS, None, "2026-03-04T15:00:00.000001+00:00"),
    ("neutral-no-news", "broker-ny7", NEUTRAL, [], 2101.25, "2026-03-06T18:00:00+00:00"),
    ("bullish-ties", "utc-ties", {**BEARISH, "direction": "bullish", "note": "", "expires_at": None}, NEWS[:2], None,
     "2026-03-04T14:59:59+00:00"),
    ("no-scan", None, BEARISH, NEWS, None, "2026-03-04T15:00:00+00:00"),
    ("no-scan-no-price", None, None, [], None, "2026-03-04T15:00:00+00:00"),
]


def market_cases() -> tuple[dict, list[dict]]:
    by_name = {c.name: c for c in CASES}
    scans, out = {}, []
    for name, case_name, bias, news, price, now in MARKETS:
        key = None
        if case_name:
            case = by_name[case_name]
            cfg = ScanConfig(lookback=case.lookback, params=DetectorParams(**case.params))
            m1 = read_input(case)
            key = f"{case_name}/{bias['direction'] if bias else 'none'}"
            if key not in scans:
                result = scan(m1, cfg, float(m1["close"].iloc[-1]))
                scans[key] = _clean(result.to_dict(TradeBias.from_dict(bias)))
        frozen = freeze(scans.get(key), bias, news, "FX:XAUUSD", "tradingview", price, datetime.fromisoformat(now))
        out.append({"name": name, "input": case_name, "scan": key, "bias": bias, "news": news, "symbol": "FX:XAUUSD",
                    "source": "tradingview", "price": price, "now": now, "expected": _clean(frozen)})
    return scans, out


def reference() -> dict:
    scans, markets = market_cases()
    return {"bias": bias_cases(), "scans": scans, "market": markets}


def test_bias_and_frozen_market_match_their_reference():
    now = reference()
    if os.environ.get("UPDATE_GOLDEN") or not PATH.exists():
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(now, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        if not os.environ.get("UPDATE_GOLDEN"):
            pytest.fail(f"wrote {PATH}: review and commit it")
        return
    assert json.loads(PATH.read_text(encoding="utf-8")) == now, (
        "The bias or the frozen market changed. If that's meant, run\n"
        "  UPDATE_GOLDEN=1 uv run pytest tests/test_market_golden.py\nand review the diff")


def test_the_cases_cover_what_they_say():
    cases = bias_cases()
    assert any(c["error"] for c in cases) and any(c["saved"] is None and not c["error"] for c in cases)
    assert {c["saved"]["expiry"] for c in cases if c["saved"]} == {"day", "week", "none"}
    _, markets = market_cases()
    frozen = {m["name"]: m["expected"] for m in markets}
    assert frozen["no-bias"]["bias"] is None and frozen["bearish"]["bias"] == {
        "direction": "bearish", "note": "CPI hot", "expires_at": "2026-03-06T22:00:00+00:00"}
    assert [e["title"] for e in frozen["no-bias"]["news"]] == ["At the minute", "ISM Services PMI", "Crude Oil Inventories"]
    assert frozen["bearish"]["setups"]["sell"] and all(s["risk"] == "on" for s in frozen["bearish"]["setups"]["sell"])
    assert frozen["no-scan"]["setups"] == {"sell": [], "buy": []} and frozen["no-scan"]["price"] is None
