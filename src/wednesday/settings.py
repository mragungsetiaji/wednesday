"""Data source settings: what the dashboard edits and the store persists.

Resolution order for each field: explicit command-line flag > settings saved
from the dashboard > environment / .env > built-in default. A source this
version no longer has (MetaTrader 5, since removed) falls back to the default.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, fields

from .quarters import clock_error
from .storage import Store

log = logging.getLogger(__name__)

SETTINGS_KEY = "data_source"

# The data sources the dashboard offers.
SOURCES = {
    "tradingview": {
        "title": "TradingView",
        "description": "Free, no account. Spot gold from TradingView's feeds: FX:XAUUSD is FXCM, or any symbol with its "
        "exchange prefix (OANDA:XAUUSD, ...). About 5 days of 1-minute bars at first; history grows as bars are stored. "
        "Unofficial: it may stop working if TradingView changes its site.",
        "default_symbol": "FX:XAUUSD",
        "default_clock": "UTC",
        "default_tick": 1,  # read from a quote stream the helper keeps open, so polling is cheap
        "min_tick": 0.5,
    },
}
# Sources for development and tests only (``--source synthetic``, ``make dev``): not offered in
# the dashboard, and not real market data.
DEV_SOURCES = {
    "synthetic": {
        "title": "Synthetic data",
        "description": "Random-walk prices for development and tests. Not real market data.",
        "default_symbol": "XAUUSD",
        "default_clock": "UTC",
        "default_tick": 2,
        "min_tick": 0.5,
    },
}
ALL_SOURCES = {**SOURCES, **DEV_SOURCES}

DEFAULT_SOURCE = "tradingview"
# Live price between the minute scans, in seconds. 0 (or a full minute) means none: the
# price then moves with each scan only.
MAX_TICK = 60


@dataclass
class DataSettings:
    source: str = DEFAULT_SOURCE
    symbol: str | None = None  # None = the source's default symbol
    clock: str | None = None  # the feed's clock; None = the source's default (see quarters.py)
    tick_seconds: float | None = None  # live price interval; None = the source's default, 0 = off

    @property
    def resolved_symbol(self) -> str:
        return self.symbol or ALL_SOURCES[self.source]["default_symbol"]

    @property
    def resolved_clock(self) -> str:
        return self.clock or ALL_SOURCES.get(self.source, SOURCES[DEFAULT_SOURCE])["default_clock"]

    @property
    def resolved_tick(self) -> float:
        info = ALL_SOURCES.get(self.source, SOURCES[DEFAULT_SOURCE])
        return info["default_tick"] if self.tick_seconds is None else self.tick_seconds

    def validate(self) -> list[str]:
        errors = []
        if self.source not in ALL_SOURCES:
            errors.append(f"Unknown source {self.source!r}")
        if self.symbol is not None and not self.symbol.strip():
            errors.append("Symbol can't be blank")
        if self.clock is not None and (err := clock_error(self.clock)):
            errors.append(err)
        if self.tick_seconds is not None and self.source in ALL_SOURCES:
            t, low = self.tick_seconds, ALL_SOURCES[self.source]["min_tick"]
            if not (t == 0 or (low and low <= t <= MAX_TICK)):
                title = ALL_SOURCES[self.source]["title"]
                errors.append(f"Live price interval for {title}: 0 (off) or {low:g} to {MAX_TICK} seconds"
                              if low else f"{title} has no live price; leave the interval at 0")
        return errors

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DataSettings:
        known = {f.name for f in fields(cls)}  # drops keys of removed sources (mt5_login, csv_path, ...)
        clean = {k: (v if v not in ("",) else None) for k, v in d.items() if k in known}
        if clean.get("tick_seconds") is not None:
            clean["tick_seconds"] = float(clean["tick_seconds"])
        return cls(**clean)


def source_availability(source: str) -> tuple[bool, str | None]:
    """Whether a source can run on this machine, and why not."""
    if source == "tradingview":
        from .feeds import tradingview_command

        command, why = tradingview_command()
        if command is None:
            return False, why
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


def _known_source(source: str | None, where: str) -> str | None:
    """``source``, or None when this version doesn't have it (MetaTrader 5, Yahoo Finance or a CSV file,
    from older versions): the default is used."""
    if source and source not in ALL_SOURCES:
        log.warning("data source %r from %s isn't available any more; using %s", source, where, DEFAULT_SOURCE)
        return None
    return source


def from_env() -> DataSettings:
    source = _known_source(_env("XAU_SOURCE"), "XAU_SOURCE")
    return DataSettings(
        source=source or DEFAULT_SOURCE,
        symbol=_env("XAU_SYMBOL") if source else None,
        clock=_env("XAU_CLOCK") if source else None,
        tick_seconds=float(tick) if (tick := _env("XAU_TICK")) else None,
    )


def resolve(cli: dict, store: Store | None) -> DataSettings:
    """Merge explicit CLI values (None = not given) over saved settings over the environment."""
    base = from_env()
    saved = store.get_setting(SETTINGS_KEY) if store else None
    if saved and saved.get("source") and not _known_source(saved["source"], "the saved settings"):
        saved = None  # e.g. Yahoo with GC=F: start over on the default source
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
        base.symbol = None  # don't carry one source's symbol over to another
        if "clock" not in explicit:
            base.clock = None  # nor its clock
        if "tick_seconds" not in explicit:
            base.tick_seconds = None  # nor its live price interval
    return DataSettings.from_dict({**base.to_dict(), **explicit})

