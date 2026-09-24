import sys
import types

import numpy as np
import pandas as pd
import pytest

from xau_screener import cli
from xau_screener.feeds import MT5Feed


class FakeMT5(types.ModuleType):
    """Minimal stand-in for the MetaTrader5 module."""

    TIMEFRAME_M1 = 1

    def __init__(self):
        super().__init__("MetaTrader5")
        self.init_calls = []
        self.fail_next_copy = 0
        self.symbols = {"XAUUSD"}

    def initialize(self, *args, **kwargs):
        self.init_calls.append((args, kwargs))
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (-1, "fake error")

    def symbol_select(self, symbol, enable):
        return symbol in self.symbols

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        assert start == 1  # closed bars only
        if self.fail_next_copy:
            self.fail_next_copy -= 1
            return None
        t0 = int(pd.Timestamp("2026-03-02 10:00").timestamp())
        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                 ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")]
        rates = np.zeros(count, dtype=dtype)
        rates["time"] = t0 + 60 * np.arange(count)
        rates["open"] = 2650.0
        rates["high"] = 2651.0
        rates["low"] = 2649.0
        rates["close"] = 2650.5
        rates["tick_volume"] = 10
        return rates

    def symbol_info_tick(self, symbol):
        return types.SimpleNamespace(bid=2650.7, ask=2651.0)


ENV_KEYS = ("XAU_SOURCE", "XAU_SYMBOL", "XAU_JSON_OUT", "XAU_LOG_FILE",
            "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # setenv first so monkeypatch restores (removes) whatever load_env_file adds during the test.
    for key in ENV_KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


@pytest.fixture
def fake_mt5(monkeypatch):
    mod = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", mod)
    return mod


def test_mt5_feed_fetch(fake_mt5):
    feed = MT5Feed("XAUUSD", login=123, password="pw", server="Broker-Demo", path=r"C:\mt5\terminal64.exe")
    feed.connect()
    args, kwargs = fake_mt5.init_calls[0]
    assert args == (r"C:\mt5\terminal64.exe",)
    assert kwargs["login"] == 123 and kwargs["server"] == "Broker-Demo"

    df = feed.fetch_m1(10)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 10 and df.index[0] == pd.Timestamp("2026-03-02 10:00")
    assert df["volume"].iloc[0] == 10
    assert feed.last_price() == 2650.7


def test_mt5_feed_reconnects_when_fetch_fails(fake_mt5):
    feed = MT5Feed("XAUUSD")
    feed.connect()
    fake_mt5.fail_next_copy = 1
    assert len(feed.fetch_m1(5)) == 5
    assert len(fake_mt5.init_calls) == 2


def test_mt5_feed_raises_after_failed_reconnect(fake_mt5):
    feed = MT5Feed("XAUUSD")
    feed.connect()
    fake_mt5.fail_next_copy = 2
    with pytest.raises(RuntimeError, match="no M1 data"):
        feed.fetch_m1(5)


def test_mt5_feed_unknown_symbol(fake_mt5):
    with pytest.raises(RuntimeError, match="Market Watch"):
        MT5Feed("GOLDX").connect()


def test_env_file_and_cli_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nXAU_SYMBOL=XAUUSD.m\nMT5_LOGIN=555\nMT5_SERVER="Broker-Live"\n', encoding="utf-8")

    args = cli.parse_args(["--env-file", str(env)])
    assert args.symbol == "XAUUSD.m"
    assert args.mt5_login == 555
    assert args.mt5_server == "Broker-Live"
    assert args.source == "mt5"

    args = cli.parse_args(["--env-file", str(env), "--symbol", "GOLD"])
    assert args.symbol == "GOLD"


def test_check_mode_with_fake_mt5(fake_mt5, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    fake_mt5.terminal_info = lambda: types.SimpleNamespace(name="MetaTrader 5", connected=True)
    fake_mt5.account_info = lambda: types.SimpleNamespace(login=123, server="Broker-Demo")
    fake_mt5.symbol_info = lambda s: types.SimpleNamespace(digits=2, bid=2650.7, ask=2651.0)
    fake_mt5.version = lambda: (500, 4500, "01 Jan 2026")
    cli.run(cli.parse_args(["--source", "mt5", "--check", "--timeframes", "5M", "--lookback", "10"]))
    out = capsys.readouterr().out
    assert "123 @ Broker-Demo" in out
    assert "OK: feed is working" in out
