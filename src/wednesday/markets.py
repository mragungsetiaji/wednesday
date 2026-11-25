"""Another market in a chart pane: candles and live price only, from TradingView.

The scan, levels, alerts, bias and drawings follow the data source's symbol. A full-screen
pane can show another symbol beside it (``FX:XAGUSD`` next to gold) through its own
TradingView connection (``tradingview.py``); nothing about it is scanned or stored.
Times are converted to the feed's clock so the panes line up.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable

import pandas as pd

from .feeds import TradingViewError, _ProcessHelper
from .tradingview import Connection
from .quarters import to_utc, utc_to_feed
from .timeframes import OHLCV_COLUMNS, Timeframe

# TradingView symbols carry an exchange prefix: FX:XAGUSD, OANDA:XAUUSD, TVC:GOLD.
SYMBOL_RE = re.compile(r"^[A-Z0-9_.]{1,32}:[A-Z0-9_.!&-]{1,32}$")
CACHE_SECONDS = 5  # panes on the same symbol and timeframe share one request
TICK_SECONDS = 0.5  # every pane's live price poll within this reads one quote


class MarketError(Exception):
    """A request the dashboard should show as is: ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def clean_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise MarketError(400, f"{symbol!r} isn't a TradingView symbol. Use the exchange prefix, e.g. FX:XAGUSD.")
    return s


MARKET_TEXT = {"label": 16, "name": 64, "venue": 32}  # longest each may be


def clean_market(m: dict) -> dict:
    """A markets list row from the dashboard: a TradingView symbol, a short label, an optional
    name and venue, keep_live a bool. ValueError says what is wrong."""
    try:
        symbol = clean_symbol(str(m.get("symbol") or ""))
    except MarketError as exc:
        raise ValueError(str(exc)) from exc
    row = {"symbol": symbol}
    for key, longest in MARKET_TEXT.items():
        value = m.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        value = (value or "").strip() or None
        if value and len(value) > longest:
            raise ValueError(f"{key} is longer than {longest} characters")
        row[key] = value
    if not row["label"]:
        raise ValueError(f"Give {symbol} a label")
    live = m.get("keep_live", False)
    if not isinstance(live, bool):
        raise ValueError("keep_live must be true or false")
    row["keep_live"] = live
    return row


class Markets:
    """One TradingView connection for every pane's own symbol, opened on first use. ``command``
    puts a stand-in process in TradingView's place (tests): it returns (command, None) or
    (None, why it can't run)."""

    def __init__(self, command: Callable[[], tuple[list[str] | None, str | None]] | None = None):
        self._command = command
        self._lock = threading.Lock()
        self._helper: Connection | _ProcessHelper | None = None
        self._cache: dict[tuple, tuple[float, object]] = {}

    def _call(self, op: str, timeout: float, **args):
        with self._lock:
            if self._helper is None and self._command is None:
                self._helper = Connection()
            elif self._helper is None:
                command, why = self._command()
                if command is None:
                    raise MarketError(503, f"Other symbols come from TradingView. {why}")
                self._helper = _ProcessHelper(command)
            try:
                return self._helper.call(op, timeout, **args)
            except TradingViewError as exc:
                if exc.code == "SYMBOL_ERROR":
                    raise MarketError(404, f"TradingView has no symbol {args.get('symbol')}. Use the exchange "
                                           "prefix, e.g. FX:XAGUSD (FXCM silver) or OANDA:XAUUSD.") from exc
                raise MarketError(502, str(exc)) from exc
            except RuntimeError as exc:
                raise MarketError(503, str(exc)) from exc

    def _cached(self, key: tuple, ttl: float, fetch: Callable[[], object]):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fetch()
        self._cache[key] = (now, value)
        if len(self._cache) > 64:  # forget the oldest, e.g. symbols typed and left
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:16]:
                del self._cache[k]
        return value

    def info(self, symbol: str) -> dict:
        """TradingView's description of ``symbol`` (full_name, description, exchange, pricescale);
        MarketError 404 when it has no such symbol."""
        return self._cached(("info", symbol), 3600, lambda: self._call("info", 30, symbol=symbol)) or {}

    def precision(self, symbol: str) -> int:
        """Decimals the symbol is quoted with (2 for gold, 3 for silver on FXCM)."""
        scale = self.info(symbol).get("pricescale") or 100
        return max(0, min(8, round(math.log10(scale))))

    def candles(self, symbol: str, tf: Timeframe, limit: int, clock: str,
                before: int | None = None) -> pd.DataFrame:
        """The latest ``limit`` ``tf`` candles (the forming one included, as the main chart does), or the
        ones opening before ``before`` (unix, feed clock). Indexed by open time on the feed's clock."""
        args = {"symbol": symbol, "timeframe": str(tf.minutes), "count": limit}
        if before is not None:
            utc = to_utc(pd.DatetimeIndex([pd.Timestamp(before, unit="s")]), clock)[0]
            args["to"] = (before if pd.isna(utc) else int(utc.timestamp())) - 1  # NaT: an hour DST repeats
        rows = self._cached(("candles", symbol, tf.name, limit, before), CACHE_SECONDS,
                            lambda: self._call("candles", 60, **args))
        df = pd.DataFrame(rows or [], columns=["time", *OHLCV_COLUMNS])
        times = pd.to_datetime(df.pop("time"), unit="s", utc=True)
        df.index = pd.DatetimeIndex([utc_to_feed(t, clock) for t in times])
        if before is not None:
            df = df[df.index < pd.Timestamp(before, unit="s")]
        return df

    def tick(self, symbol: str, clock: str) -> dict:
        """The live price and when it traded (unix, feed clock); price None when there is none yet."""
        q = self._cached(("quote", symbol), TICK_SECONDS, lambda: self._call("quote", 15, symbol=symbol))
        if not q or q.get("price") is None:
            return {"symbol": symbol, "price": None, "time": None}
        at = pd.Timestamp(int(q["time"]), unit="s", tz="UTC") if q.get("time") else pd.Timestamp.now(tz="UTC")
        feed_time = utc_to_feed(at, clock)
        return {"symbol": symbol, "price": float(q["price"]), "time": int(feed_time.timestamp())}

    def close(self) -> None:
        with self._lock:
            if self._helper is not None:
                self._helper.close()
                self._helper = None
