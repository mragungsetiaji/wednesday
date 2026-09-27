"""Data source settings: what the dashboard edits and the store persists.

Resolution order for each field: explicit command-line flag > settings saved
from the dashboard > environment / .env > built-in default. The MT5 password
never goes into the database: it is kept in the operating system's credential
store (see ``secret_store``), or read from ``MT5_PASSWORD`` for setups that
still use it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .mt5_terminals import normalize_path
from .quarters import clock_error
from .storage import Store

SETTINGS_KEY = "data_source"

SOURCES = {
    "yfinance": {
        "title": "Yahoo Finance",
        "description": "Free, no account. COMEX gold futures (GC=F), not spot, and only the last ~7 days of 1-minute bars; "
        "history grows as the screener keeps storing bars.",
        "default_symbol": "GC=F",
        "default_clock": "UTC",
        "default_tick": 60,
        "min_tick": 15,  # every tick is a request to Yahoo; faster risks being rate limited
    },
    "mt5": {
        "title": "MetaTrader 5",
        "description": "Your broker's XAUUSD from a running MT5 terminal on the same Windows machine. Candles match the MT5 chart.",
        "default_symbol": "XAUUSD",
        "default_clock": "NY+7",
        "default_tick": 1,
        "min_tick": 0.5,
    },
    "csv": {
        "title": "CSV file",
        "description": "M1 bars from a CSV (time, open, high, low, close[, volume]) that another process keeps appending to.",
        "default_symbol": "XAUUSD",
        "default_clock": "UTC",
        "default_tick": 0,  # a CSV has closed bars only, no live price
        "min_tick": 0,
    },
    "synthetic": {
        "title": "Demo data",
        "description": "Random-walk prices for trying the dashboard. Not real market data.",
        "default_symbol": "XAUUSD",
        "default_clock": "UTC",
        "default_tick": 2,
        "min_tick": 0.5,
    },
}

DEFAULT_SOURCE = "yfinance"
# Live price between the minute scans, in seconds. 0 (or a full minute) means none: the
# price then moves with each scan only.
MAX_TICK = 60


@dataclass
class DataSettings:
    source: str = DEFAULT_SOURCE
    symbol: str | None = None  # None = the source's default symbol
    csv_path: str | None = None
    mt5_login: int | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None
    clock: str | None = None  # the feed's clock; None = the source's default (see quarters.py)
    tick_seconds: float | None = None  # live price interval; None = the source's default, 0 = off

    @property
    def resolved_symbol(self) -> str:
        return self.symbol or SOURCES[self.source]["default_symbol"]

    @property
    def resolved_clock(self) -> str:
        return self.clock or SOURCES.get(self.source, SOURCES[DEFAULT_SOURCE])["default_clock"]

    @property
    def resolved_tick(self) -> float:
        info = SOURCES.get(self.source, SOURCES[DEFAULT_SOURCE])
        return info["default_tick"] if self.tick_seconds is None else self.tick_seconds

    def validate(self) -> list[str]:
        errors = []
        if self.source not in SOURCES:
            errors.append(f"Unknown source {self.source!r}")
        if self.source == "csv" and not self.csv_path:
            errors.append("A CSV file path is required for the CSV source")
        if self.symbol is not None and not self.symbol.strip():
            errors.append("Symbol can't be blank")
        if self.clock is not None and (err := clock_error(self.clock)):
            errors.append(err)
        if self.tick_seconds is not None and self.source in SOURCES:
            t, low = self.tick_seconds, SOURCES[self.source]["min_tick"]
            if not (t == 0 or (low and low <= t <= MAX_TICK)):
                title = SOURCES[self.source]["title"]
                errors.append(f"Live price interval for {title}: 0 (off) or {low:g} to {MAX_TICK} seconds"
                              if low else f"{title} has no live price; leave the interval at 0")
        return errors

    def terminal_error(self) -> str | None:
        """Checked when saving from the dashboard only, so a terminal removed later can't stop the app starting."""
        if self.source == "mt5" and self.mt5_path and sys.platform == "win32" and not Path(self.mt5_path).is_file():
            return f"No MT5 terminal at {self.mt5_path}. Pick one from the list or with Browse."
        return None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DataSettings:
        known = {f.name for f in fields(cls)}
        clean = {k: (v if v not in ("",) else None) for k, v in d.items() if k in known}
        if clean.get("mt5_login") is not None:
            clean["mt5_login"] = int(clean["mt5_login"])
        if clean.get("tick_seconds") is not None:
            clean["tick_seconds"] = float(clean["tick_seconds"])
        if "mt5_path" in clean:
            clean["mt5_path"] = normalize_path(clean["mt5_path"])
        return cls(**clean)


def source_availability(source: str) -> tuple[bool, str | None]:
    """Whether a source can run on this machine, and why not."""
    if source == "mt5":
        if sys.platform != "win32":
            return False, "Needs Windows with the MT5 terminal installed."
        if importlib.util.find_spec("MetaTrader5") is None:
            return False, "Install it with: uv sync --extra mt5"
    if source == "yfinance" and importlib.util.find_spec("yfinance") is None:
        return False, "Install it with: uv sync"
    return True, None


def catalog() -> list[dict]:
    out = []
    for key, info in SOURCES.items():
        ok, reason = source_availability(key)
        out.append({"id": key, **info, "available": ok, "unavailable_reason": reason})
    return out


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def from_env() -> DataSettings:
    login = _env("MT5_LOGIN")
    return DataSettings(
        source=_env("XAU_SOURCE") or DEFAULT_SOURCE,
        symbol=_env("XAU_SYMBOL"),
        csv_path=_env("XAU_CSV"),
        mt5_login=int(login) if login else None,
        mt5_server=_env("MT5_SERVER"),
        mt5_path=_env("MT5_PATH"),
        clock=_env("XAU_CLOCK"),
        tick_seconds=float(tick) if (tick := _env("XAU_TICK")) else None,
    )


def resolve(cli: dict, store: Store | None) -> DataSettings:
    """Merge explicit CLI values (None = not given) over saved settings over the environment."""
    base = from_env()
    saved = store.get_setting(SETTINGS_KEY) if store else None
    if saved:
        base = DataSettings.from_dict({**base.to_dict(), **{k: v for k, v in saved.items() if v is not None}})
        # A saved source switch also resets the symbol and clock unless they were saved with it.
        if saved.get("source") and saved.get("symbol") is None:
            base.symbol = None
        if saved.get("source") and saved.get("clock") is None:
            base.clock = None
        if saved.get("source") and saved.get("tick_seconds") is None:
            base.tick_seconds = None
    explicit = {k: v for k, v in cli.items() if v is not None}
    if explicit.get("source") and explicit["source"] != base.source and "symbol" not in explicit:
        base.symbol = None  # don't carry e.g. GC=F over to MT5
        if "clock" not in explicit:
            base.clock = None  # nor Yahoo's UTC clock
        if "tick_seconds" not in explicit:
            base.tick_seconds = None  # nor MT5's one-second ticks to Yahoo
    return DataSettings.from_dict({**base.to_dict(), **explicit})


# MT5 password: one per account, in the OS credential store (see secret_store.py).
MT5_SERVICE = "Wednesday MT5"


def mt5_account(login: int | None, server: str | None) -> str:
    return f"{login}@{server or ''}"
