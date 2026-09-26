"""Data source settings: what the dashboard edits and the store persists.

Resolution order for each field: explicit command-line flag > settings saved
from the dashboard > environment / .env > built-in default. Secrets (the MT5
password) never go into the database; they come from the environment only.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import asdict, dataclass, fields

from .storage import Store

SETTINGS_KEY = "data_source"

SOURCES = {
    "yfinance": {
        "title": "Yahoo Finance",
        "description": "Free, no account. COMEX gold futures (GC=F), not spot, and only the last ~7 days of 1-minute bars; "
        "history grows as the screener keeps storing bars.",
        "default_symbol": "GC=F",
    },
    "mt5": {
        "title": "MetaTrader 5",
        "description": "Your broker's XAUUSD from a running MT5 terminal on the same Windows machine. Candles match the MT5 chart.",
        "default_symbol": "XAUUSD",
    },
    "csv": {
        "title": "CSV file",
        "description": "M1 bars from a CSV (time, open, high, low, close[, volume]) that another process keeps appending to.",
        "default_symbol": "XAUUSD",
    },
    "synthetic": {
        "title": "Demo data",
        "description": "Random-walk prices for trying the dashboard. Not real market data.",
        "default_symbol": "XAUUSD",
    },
}

DEFAULT_SOURCE = "yfinance"


@dataclass
class DataSettings:
    source: str = DEFAULT_SOURCE
    symbol: str | None = None  # None = the source's default symbol
    csv_path: str | None = None
    mt5_login: int | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None

    @property
    def resolved_symbol(self) -> str:
        return self.symbol or SOURCES[self.source]["default_symbol"]

    def validate(self) -> list[str]:
        errors = []
        if self.source not in SOURCES:
            errors.append(f"Unknown source {self.source!r}")
        if self.source == "csv" and not self.csv_path:
            errors.append("A CSV file path is required for the CSV source")
        if self.symbol is not None and not self.symbol.strip():
            errors.append("Symbol can't be blank")
        return errors

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DataSettings:
        known = {f.name for f in fields(cls)}
        clean = {k: (v if v not in ("",) else None) for k, v in d.items() if k in known}
        if clean.get("mt5_login") is not None:
            clean["mt5_login"] = int(clean["mt5_login"])
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
    )


def resolve(cli: dict, store: Store | None) -> DataSettings:
    """Merge explicit CLI values (None = not given) over saved settings over the environment."""
    base = from_env()
    saved = store.get_setting(SETTINGS_KEY) if store else None
    if saved:
        base = DataSettings.from_dict({**base.to_dict(), **{k: v for k, v in saved.items() if v is not None}})
        # A saved source switch also resets the symbol unless one was saved with it.
        if saved.get("source") and saved.get("symbol") is None:
            base.symbol = None
    explicit = {k: v for k, v in cli.items() if v is not None}
    if explicit.get("source") and explicit["source"] != base.source and "symbol" not in explicit:
        base.symbol = None  # don't carry e.g. GC=F over to MT5
    return DataSettings.from_dict({**base.to_dict(), **explicit})
