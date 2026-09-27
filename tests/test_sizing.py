"""Position size per setup (issue #21): lots round down to the broker step, min lot over budget is flagged."""

import sys
import types

import pytest
from fastapi.testclient import TestClient

from wednesday.bias import TradeBias
from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.feeds import MT5Feed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.sizing import RiskSettings, floor_to_step, resolve, size_text

from test_mt5_and_cli import FakeMT5


def test_floor_to_step():
    assert floor_to_step(0.359, 0.01) == 0.35
    assert floor_to_step(0.35, 0.01) == 0.35  # no float dust pushing it down a step
    assert floor_to_step(1.29, 0.1) == 1.2
    assert floor_to_step(7.9, 1) == 7.0


def test_standard_usd_account():
    # $10,000, 1% risk, XAUUSD 100 oz: a 3.00 stop costs $300 per lot, so $100 buys 0.33 lot.
    sizer = resolve(RiskSettings(enabled=True, balance=10_000, value=1.0), None)
    size = sizer.size(3.0)
    assert size["lots"] == 0.33 and size["risk"] == 99.0 and size["reward_2r"] == 198.0
    assert size["risk"] <= size["budget"] == 100.0
    assert size_text(size, "USD") == "0.33 lot · risk $99.00 · 2R $198.00"
    assert sizer.source == "settings"


def test_cent_style_broker_with_coarse_step():
    # Fixed $50, contract of 10 oz per lot, lots in steps of 0.1 with a 0.1 minimum.
    sizer = resolve(RiskSettings(enabled=True, mode="amount", value=50, contract_size=10, min_lot=0.1, lot_step=0.1), None)
    assert sizer.size(1.66)["lots"] == 3.0  # 50 / 16.6 = 3.01 -> 3.0
    assert sizer.size(0.0) is None


def test_min_lot_over_budget_is_flagged():
    sizer = resolve(RiskSettings(enabled=True, mode="amount", value=20, min_lot=0.1, lot_step=0.1), None)
    size = sizer.size(3.0)  # 0.1 lot x 3.00 x 100 = $30 > $20
    assert size["below_min"] is True and size["lots"] == 0.0 and size["min_lot_risk"] == 30.0
    assert "over the $20.00 budget" in size_text(size, "USD")


def test_risk_off_multiplier():
    sizer = resolve(RiskSettings(enabled=True, balance=10_000, value=1.0, risk_off_multiplier=0.5), None)
    assert sizer.size(1.0, "on")["lots"] == 1.0
    assert sizer.size(1.0, "off")["lots"] == 0.5


def test_mt5_spec_wins_and_percent_needs_a_balance():
    spec = {"balance": 2_000.0, "currency": "EUR", "per_point": 92.0, "min_lot": 0.01, "lot_step": 0.01, "max_lot": 0.05}
    sizer = resolve(RiskSettings(enabled=True, balance=10_000, value=2.0), spec)
    assert sizer.source == "mt5" and sizer.budget == 40.0 and sizer.currency == "EUR"
    assert sizer.size(0.2)["lots"] == 0.05  # capped at the broker's max
    assert size_text(sizer.size(0.1), "EUR") == "0.05 lot · risk 0.46 EUR · 2R 0.92 EUR"
    assert resolve(RiskSettings(enabled=True, value=1.0), None) is None  # percent of an unknown balance
    assert resolve(RiskSettings(enabled=False, balance=1000), None) is None


def test_settings_validation():
    with pytest.raises(ValueError):
        RiskSettings.from_dict({"mode": "percent", "value": 150})
    with pytest.raises(ValueError):
        RiskSettings.from_dict({"lot_step": 0})
    with pytest.raises(ValueError):
        RiskSettings.from_dict({"risk_off_multiplier": 2})


class SpecMT5(FakeMT5):
    def account_info(self):
        return types.SimpleNamespace(balance=5_000.0, currency="USD")

    def symbol_info(self, symbol):
        return types.SimpleNamespace(trade_tick_size=0.01, trade_tick_value=1.0, point=0.01, trade_contract_size=100.0,
                                     volume_min=0.01, volume_step=0.01, volume_max=50.0)


def test_mt5_trading_spec(monkeypatch):
    monkeypatch.setitem(sys.modules, "MetaTrader5", SpecMT5())
    feed = MT5Feed("XAUUSD")
    feed.connect()
    spec = feed.trading_spec()
    assert spec["per_point"] == 100.0 and spec["balance"] == 5_000.0 and spec["lot_step"] == 0.01


def test_scan_setups_carry_sizes(tmp_path):
    runtime = Runtime(ScanConfig(lookback=60, params=DetectorParams(swing_length=2)), DataSettings(source="synthetic"), None)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    runtime.engine.feed.connect()
    runtime.engine.step()
    scan = api.get("/api/scan").json()["scan"]
    assert scan["sizing"] is None and all(s["size"] is None for s in scan["setups"]["sell"] + scan["setups"]["buy"])

    assert api.put("/api/risk", json={"enabled": True, "value": 500}).status_code == 422
    body = api.put("/api/risk", json={"enabled": True, "mode": "amount", "value": 100}).json()
    assert body["sizer"]["budget"] == 100 and body["mt5"] is None
    runtime.set_bias(TradeBias("bearish"))
    scan = api.get("/api/scan").json()["scan"]
    setups = scan["setups"]["sell"] + scan["setups"]["buy"]
    assert setups, "the synthetic scan should have setups"
    for s in setups:
        size = s["size"]
        budget = 100 * (0.5 if s["risk"] == "off" else 1)
        assert size["budget"] == budget
        assert size["below_min"] or 0 < size["risk"] <= budget


def test_alert_carries_the_size_line():
    import pandas as pd

    from wednesday.alerts import format_alert
    from wednesday.levels import Level

    t = pd.Timestamp("2026-03-02 10:00")
    ob = Level("ob", "bearish", 2652.0, 2650.0, t, t, label="BEAR OB",
               meta={"entry": 2650.0, "sl": 2652.0, "risk": 2.0, "priority": "extreme"})
    sizer = resolve(RiskSettings(enabled=True, mode="amount", value=100), None)
    text = format_alert("XAUUSD", "5M", ob, 2648.0, None, trade_bias=TradeBias("bullish"), sizer=sizer)
    assert "Size: 0.25 lot · risk $50.00 · 2R $100.00" in text  # against the bias: half size
