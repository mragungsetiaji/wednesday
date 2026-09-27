"""1-minute data feeds.

Every feed returns *closed* M1 bars as an OHLCV DataFrame indexed by bar open
time. Add a new broker/API by subclassing :class:`DataFeed`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from .timeframes import M1, OHLCV_COLUMNS, Timeframe, normalize_ohlcv

if TYPE_CHECKING:
    from .settings import DataSettings
    from .storage import Store

log = logging.getLogger(__name__)

# The MetaTrader5 module's constant for each chart timeframe.
MT5_TIMEFRAMES = {"5M": "TIMEFRAME_M5", "15M": "TIMEFRAME_M15", "30M": "TIMEFRAME_M30",
                  "1H": "TIMEFRAME_H1", "4H": "TIMEFRAME_H4"}


class DataFeed(ABC):
    name = "base"
    persist = True  # store fetched bars in the database
    # Keep retrying a failed connection every minute. MT5 doesn't: connecting starts the
    # terminal, so retrying would reopen it every minute after the trader closed it.
    retry_connect = True
    # fetch_history serves each timeframe's own candles (the chart's older history).
    native_history = False

    def connect(self) -> None:  # noqa: B027 - optional hook
        """Open connections / log in. Called once before the first fetch."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""

    @abstractmethod
    def fetch_m1(self, count: int) -> pd.DataFrame:
        """Return the latest ``count`` closed M1 bars (oldest first)."""

    def last_price(self) -> float | None:
        """Live price if the feed has one; otherwise the scanner uses the last M1 close."""
        return None

    def trading_spec(self) -> dict | None:
        """Account balance and the symbol's contract spec, for position sizing; None when the feed has none."""
        return None

    def fetch_history(self, tf: Timeframe, before: pd.Timestamp, count: int) -> pd.DataFrame:
        """Up to ``count`` closed ``tf`` candles opening before ``before`` (oldest first).

        Only for feeds with ``native_history``; the others' history is the stored M1 bars.
        """
        raise NotImplementedError

    def describe(self) -> dict:
        """Connection details shown by ``--check``."""
        return {"feed": self.name}


class MT5Feed(DataFeed):
    """MetaTrader 5 terminal (Windows, ``uv sync --extra mt5``).

    Bar times are broker server time, so resampled 4H/1H candles line up with
    the MT5 chart.
    """

    name = "mt5"
    native_history = True
    retry_connect = False

    def __init__(self, symbol: str = "XAUUSD", login: int | None = None,
                 password: str | None = None, server: str | None = None, path: str | None = None,
                 timeout_ms: int = 60_000):
        self.symbol = symbol
        self.login, self.password, self.server, self.path = login, password, server, path
        self.timeout_ms = timeout_ms
        self._mt5 = None

    def connect(self) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:  # pragma: no cover - platform specific
            raise RuntimeError("MetaTrader5 is not installed. Run: uv sync --extra mt5 (Windows only)") from exc
        # ``path`` (terminal64.exe) is positional; credentials are optional when the terminal is logged in.
        args = [self.path] if self.path else []
        kwargs = {k: v for k, v in {"login": self.login, "password": self.password,
                                    "server": self.server}.items() if v is not None}
        if not mt5.initialize(*args, timeout=self.timeout_ms, **kwargs):
            err = mt5.last_error()
            mt5.shutdown()
            raise RuntimeError(f"MT5 initialize failed: {err}. Is the terminal installed, running and logged in?")
        if not mt5.symbol_select(self.symbol, True):
            err = mt5.last_error()
            mt5.shutdown()
            raise RuntimeError(f"MT5 symbol {self.symbol!r} not available: {err}. "
                               "Check the exact name in Market Watch (e.g. XAUUSD.m, GOLD).")
        self._mt5 = mt5

    def reconnect(self) -> None:
        """Connect again to a terminal that is still open; never start one the trader closed."""
        log.warning("reconnecting to MT5")
        self.close()
        from .mt5_terminals import is_running

        if self.path and not is_running(self.path):
            raise RuntimeError("The MT5 terminal is closed. Open it, log in, then press Reconnect in Settings.")
        self.connect()

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    def _copy_rates(self, count: int):
        mt5 = self._mt5
        # Position 0 is the still-forming bar; start at 1 for closed bars only.
        return mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 1, count)

    def fetch_m1(self, count: int) -> pd.DataFrame:
        if self._mt5 is None:
            self.reconnect()  # the connection was lost earlier
        rates = self._copy_rates(count)
        if rates is None or len(rates) == 0:
            # Terminal restarted or lost its connection: re-initialise once and retry.
            self.reconnect()
            rates = self._copy_rates(count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 returned no M1 data: {self._mt5.last_error()}")
        if len(rates) < count:
            log.warning("MT5 returned %d of %d requested M1 bars; raise Tools > Options > Charts > "
                        "'Max bars in chart' or let the terminal download more history", len(rates), count)
        df = pd.DataFrame(rates)
        df.index = pd.to_datetime(df["time"], unit="s")  # broker server time
        df = df.rename(columns={"tick_volume": "volume"})
        return normalize_ohlcv(df)

    def fetch_m1_range(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """M1 bars opening in [start, end] (server time), for the Lab's backfill."""
        if self._mt5 is None:
            self.reconnect()
        mt5 = self._mt5
        rates = mt5.copy_rates_range(self.symbol, mt5.TIMEFRAME_M1, int(start.timestamp()), int(end.timestamp()))
        if rates is None:
            raise RuntimeError(f"MT5 returned no M1 history: {mt5.last_error()}")
        if len(rates) == 0:
            return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([]), dtype=float)
        df = pd.DataFrame(rates)
        df.index = pd.to_datetime(df["time"], unit="s")
        return normalize_ohlcv(df.rename(columns={"tick_volume": "volume"}))

    def trading_spec(self) -> dict | None:
        """Balance and lot rules from the terminal, and the money one lot makes per 1.00 move."""
        mt5 = self._mt5
        acc = mt5.account_info() if mt5 else None
        sym = mt5.symbol_info(self.symbol) if mt5 else None
        if not acc or not sym:
            return None
        tick = sym.trade_tick_size or sym.point
        # Tick value is in the account currency; some brokers report 0 while the market is closed.
        per_point = sym.trade_tick_value / tick if tick and sym.trade_tick_value > 0 else sym.trade_contract_size
        return {"balance": float(acc.balance), "currency": acc.currency, "per_point": float(per_point),
                "contract_size": float(sym.trade_contract_size), "min_lot": float(sym.volume_min),
                "lot_step": float(sym.volume_step), "max_lot": float(sym.volume_max) or None}

    def fetch_history(self, tf: Timeframe, before: pd.Timestamp, count: int) -> pd.DataFrame:
        """The terminal's own ``tf`` candles, so scrolling back needs no M1 history."""
        if self._mt5 is None:
            self.reconnect()
        mt5 = self._mt5
        frame = getattr(mt5, MT5_TIMEFRAMES[tf.name])
        # Times are broker server time as unix seconds, both ways: pass ``before`` as-is.
        rates = mt5.copy_rates_from(self.symbol, frame, int(before.timestamp()) - 1, count)
        if rates is None:
            raise RuntimeError(f"MT5 returned no {tf.name} history: {mt5.last_error()}")
        if len(rates) == 0:
            return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([]), dtype=float)
        df = pd.DataFrame(rates)
        df.index = pd.to_datetime(df["time"], unit="s")
        df = normalize_ohlcv(df.rename(columns={"tick_volume": "volume"}))
        return df[df.index < before]

    def last_price(self) -> float | None:
        tick = self._mt5.symbol_info_tick(self.symbol) if self._mt5 else None
        return float(tick.bid) if tick else None

    def account_history(self) -> dict:
        """The logged-in account, every deal in its history and the open positions (for the journal).

        Runs on the scan thread (see ``Engine.call``). Only the account number and
        server are read about the account, never its password or the holder's name.
        """
        mt5 = self._mt5
        if mt5 is None:
            raise RuntimeError("The MT5 terminal isn't connected yet")
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError(f"MT5 has no account logged in: {mt5.last_error()}")
        deals = mt5.history_deals_get(datetime(2000, 1, 1), datetime.now() + timedelta(days=3))
        if deals is None:
            raise RuntimeError(f"MT5 returned no deal history: {mt5.last_error()}")
        return {
            "account": {"login": str(acc.login), "server": acc.server, "company": acc.company,
                        "currency": acc.currency, "balance": acc.balance, "equity": acc.equity},
            "deals": [d._asdict() for d in deals],
            "positions": [p._asdict() for p in (mt5.positions_get() or ())],
        }

    def describe(self) -> dict:
        """Connection details for ``--check``."""
        mt5 = self._mt5
        term, acc, sym = mt5.terminal_info(), mt5.account_info(), mt5.symbol_info(self.symbol)
        return {
            "terminal": f"{term.name} build {mt5.version()[1]}" if term else None,
            "connected": bool(term and term.connected),
            "account": f"{acc.login} @ {acc.server}" if acc else None,
            "symbol": self.symbol,
            "digits": sym.digits if sym else None,
            "bid/ask": f"{sym.bid} / {sym.ask}" if sym else None,
        }


class YFinanceFeed(DataFeed):
    """Yahoo Finance: free, the default source.

    Yahoo only serves ~7 days of 1m bars and ``GC=F`` is COMEX gold futures,
    not spot XAUUSD, so levels differ from a broker chart by a few dollars.
    Bars are UTC. Stored history (see ``M1Buffer``) grows beyond 7 days over time.
    """

    name = "yfinance"

    def __init__(self, symbol: str = "GC=F"):
        self.symbol = symbol

    def _history(self, period: str) -> pd.DataFrame:
        import yfinance as yf

        return yf.Ticker(self.symbol).history(period=period, interval="1m", auto_adjust=False)

    def fetch_m1(self, count: int) -> pd.DataFrame:
        # Small updates only need today; the first load takes everything Yahoo has.
        df = self._history("1d" if count <= 390 else "7d")
        if df.empty and count <= 390:
            df = self._history("5d")  # e.g. just after the weekly open
        if df.empty:
            raise RuntimeError(f"Yahoo Finance returned no 1m data for {self.symbol}")
        df = df.copy()
        if df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        df = normalize_ohlcv(df)
        # Drop the still-forming minute.
        now = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("min")
        df = df[df.index < now]
        return df.tail(count)


class CSVFeed(DataFeed):
    """M1 bars from a CSV with columns time/datetime, open, high, low, close[, volume].

    The file is re-read on every fetch, so another process can keep appending to it.
    """

    name = "csv"

    def __init__(self, path: str | Path, time_column: str | None = None):
        self.path = Path(path)
        self.time_column = time_column

    def fetch_m1(self, count: int) -> pd.DataFrame:
        raw = pd.read_csv(self.path)
        raw.columns = [c.strip().lower() for c in raw.columns]
        col = self.time_column or next((c for c in ("time", "datetime", "date", "timestamp") if c in raw.columns), None)
        if col is None:
            raise ValueError(f"{self.path}: no time column found")
        raw.index = pd.to_datetime(raw.pop(col))
        return normalize_ohlcv(raw).tail(count)


class SyntheticFeed(DataFeed):
    """Random-walk gold-like prices for demos and tests. Each fetch advances one minute."""

    name = "synthetic"
    persist = False  # every run is a different random walk; storing it would splice unrelated series

    def __init__(self, start_price: float = 2650.0, seed: int | None = 42,
                 history: int = 60_000, end: pd.Timestamp | None = None):
        self._rng = np.random.default_rng(seed)
        end = (end or pd.Timestamp.now(tz="UTC").tz_localize(None)).floor("min") - M1
        index = pd.date_range(end=end, periods=history, freq="1min")
        self._bars = self._generate(index, start_price)

    def _generate(self, index: pd.DatetimeIndex, start: float) -> pd.DataFrame:
        n = len(index)
        # Regime-switching drift gives trends and pullbacks, which produce structure breaks.
        drift = np.repeat(self._rng.normal(0, 0.05, n // 240 + 1), 240)[:n]
        steps = drift + self._rng.normal(0, 0.6, n)
        close = start + np.cumsum(steps)
        open_ = np.concatenate([[start], close[:-1]])
        wick = np.abs(self._rng.normal(0, 0.35, (2, n)))
        high = np.maximum(open_, close) + wick[0]
        low = np.minimum(open_, close) - wick[1]
        vol = self._rng.integers(50, 500, n).astype(float)
        return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=index)

    def fetch_m1(self, count: int) -> pd.DataFrame:
        last = self._bars.iloc[-1]
        nxt = self._generate(pd.DatetimeIndex([self._bars.index[-1] + M1]), float(last["close"]))
        self._bars = pd.concat([self._bars, nxt]).iloc[1:]
        return self._bars.tail(count).copy()


class M1Buffer:
    """Rolling store of M1 bars: one big initial load, then small incremental updates."""

    def __init__(self, feed: DataFeed, max_bars: int, update_bars: int = 30,
                 store: Store | None = None, key: tuple[str, str] | None = None):
        self.feed = feed
        self.max_bars = max_bars
        self.update_bars = update_bars
        self.store = store
        self.key = key  # (source, symbol) the bars are stored under
        self.bars: pd.DataFrame | None = None

    def update(self) -> pd.DataFrame:
        if self.bars is None:
            # Start from stored history, so a restart doesn't refetch everything
            # and short-history feeds (Yahoo: ~7 days) keep what they collected.
            self.bars = self.store.load_bars(*self.key, self.max_bars) if self.store and self.key else None
            if self.bars is not None and self.bars.empty:
                self.bars = None
        last = self.bars.index[-1] if self.bars is not None else None

        if last is None:
            fresh = normalize_ohlcv(self.feed.fetch_m1(self.max_bars))
        else:
            fresh = normalize_ohlcv(self.feed.fetch_m1(self.update_bars))
            if not fresh.empty and fresh.index[0] > last + M1:
                # Gap bigger than the update window (restart, reconnect): fetch all the feed has.
                fresh = normalize_ohlcv(self.feed.fetch_m1(self.max_bars))

        if self.store and self.key and not fresh.empty:
            new = fresh if last is None else fresh[fresh.index > last - 5 * M1]
            self.store.save_bars(*self.key, new)

        merged = fresh if self.bars is None else pd.concat([self.bars, fresh])
        self.bars = merged[~merged.index.duplicated(keep="last")].sort_index().tail(self.max_bars)
        return self.bars


def build_feed(settings: DataSettings, mt5_password: str | None = None) -> DataFeed:
    source, symbol = settings.source, settings.resolved_symbol
    if source == "mt5":
        return MT5Feed(symbol, login=settings.mt5_login, password=mt5_password,
                       server=settings.mt5_server, path=settings.mt5_path)
    if source == "yfinance":
        return YFinanceFeed(symbol)
    if source == "csv":
        if not settings.csv_path:
            raise ValueError("A CSV path is required for the CSV source")
        return CSVFeed(settings.csv_path)
    if source == "synthetic":
        return SyntheticFeed()
    raise ValueError(f"Unknown source {source!r}")
