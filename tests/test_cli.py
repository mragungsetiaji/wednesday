"""Command line: the .env file, and flags over saved settings over the environment."""

import os

import pytest

from wednesday import cli

ENV_KEYS = ("XAU_SOURCE", "XAU_SYMBOL", "XAU_CSV", "XAU_DB_URL", "XAU_JSON_OUT", "XAU_LOG_FILE", "XAU_SERVE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # setenv first so monkeypatch restores (removes) whatever load_env_file adds during the test.
    for key in ENV_KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def cli_values(a):
    return {"source": a.source, "symbol": a.symbol}


def test_env_file_and_cli_precedence(tmp_path):
    from wednesday.settings import SETTINGS_KEY, resolve
    from wednesday.storage import Store

    env = tmp_path / ".env"
    env.write_text('# comment\nXAU_SOURCE=tradingview\nXAU_SYMBOL="OANDA:XAUUSD"\n', encoding="utf-8")
    args = cli.parse_args(["--env-file", str(env)])
    assert args.source is None and args.symbol is None  # only explicit flags are kept on args

    # Environment only.
    d = resolve(cli_values(args), None)
    assert (d.source, d.resolved_symbol) == ("tradingview", "OANDA:XAUUSD")

    # Saved dashboard settings beat the environment; switching source resets the symbol.
    store = Store(f"sqlite:///{tmp_path / 'x.db'}")
    store.set_setting(SETTINGS_KEY, {"source": "synthetic", "symbol": None})
    d = resolve(cli_values(args), store)
    assert (d.source, d.resolved_symbol) == ("synthetic", "XAUUSD")
    # A source this version doesn't have any more (Yahoo, CSV) falls back to the environment's.
    store.set_setting(SETTINGS_KEY, {"source": "yfinance", "symbol": "GC=F"})
    d = resolve(cli_values(args), store)
    assert (d.source, d.resolved_symbol) == ("tradingview", "OANDA:XAUUSD")

    # Explicit flags beat everything.
    args = cli.parse_args(["--env-file", str(env), "--source", "tradingview", "--symbol", "FX:XAGUSD"])
    d = resolve(cli_values(args), store)
    assert (d.source, d.resolved_symbol) == ("tradingview", "FX:XAGUSD")
    args = cli.parse_args(["--env-file", str(env), "--source", "synthetic"])
    assert resolve(cli_values(args), store).resolved_symbol == "XAUUSD"


def test_a_removed_source_falls_back_to_the_default(tmp_path, monkeypatch):
    """MT5, Yahoo Finance and CSV are gone: an install that had one saved starts on the default source."""
    from wednesday.settings import DEFAULT_SOURCE, SETTINGS_KEY, from_env, resolve
    from wednesday.storage import Store

    store = Store(f"sqlite:///{tmp_path / 'x.db'}")
    store.set_setting(SETTINGS_KEY, {"source": "mt5", "symbol": "XAUUSD.m", "clock": "NY+7", "mt5_login": 5})
    d = resolve({}, store)
    assert (d.source, d.resolved_symbol, d.resolved_clock) == (DEFAULT_SOURCE, "FX:XAUUSD", "UTC")
    store.set_setting(SETTINGS_KEY, {"source": "csv", "csv_path": "x.csv"})
    assert resolve({}, store).source == DEFAULT_SOURCE == "tradingview"
    assert d.validate() == []
    monkeypatch.setenv("XAU_SOURCE", "mt5")
    monkeypatch.setenv("XAU_SYMBOL", "XAUUSD.m")
    assert (from_env().source, from_env().symbol) == (DEFAULT_SOURCE, None)
    with pytest.raises(SystemExit):
        cli.parse_args(["--source", "mt5"])


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_env_file_in_the_encodings_windows_writes(tmp_path, monkeypatch, encoding):
    """Notepad adds a BOM, PowerShell 5 writes UTF-16: the first variable must still be read."""
    monkeypatch.setenv("WEDNESDAY_LICENCE_SERVER", "")  # set but empty counts as unset
    monkeypatch.setenv("XAU_SYMBOL", "")
    env = tmp_path / ".env"
    env.write_bytes("WEDNESDAY_LICENCE_SERVER=http://192.168.1.5:8790\r\nXAU_SYMBOL='GOLD'\r\n".encode(encoding))
    cli.load_env_file(env)
    assert os.environ["WEDNESDAY_LICENCE_SERVER"] == "http://192.168.1.5:8790"
    assert os.environ["XAU_SYMBOL"] == "GOLD"
    monkeypatch.setenv("XAU_SYMBOL", "XAUUSD")
    cli.load_env_file(env)
    assert os.environ["XAU_SYMBOL"] == "XAUUSD"  # a variable already set wins
