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

    def last_tick(self) -> tuple[pd.Timestamp, float] | None:
        """The live price and when it traded, on the feed's clock (naive), polled between the
        minute scans to build the forming candle. None when the feed has no live price."""
        return None

    def trading_spec(self) -> dict | None:
        """Account balance and the symbol's contract spec, for position sizing; None when the feed has none."""
        return None

    def daily_history(self, clock: str, count: int) -> pd.DataFrame | None:
        """Up to ``count`` closed daily bars indexed by New York trading day (the day a
        17:00 New York close ends), for the move distribution's long lookbacks. None when
        the feed has none, or its days don't end at the New York close."""
        return None

    def fetch_history(self, tf: Timeframe, before: pd.Timestamp, count: int) -> pd.DataFrame:
        """Up to ``count`` closed ``tf`` candles opening before ``before`` (oldest first).

        Only for feeds with ``native_history``; the others' history is the stored M1 bars.
        """
        raise NotImplementedError

    def describe(self) -> dict:
        """Connection details shown by ``--check``."""
        return {"feed": self.name}


TICK_BATCH = 5000  # most ticks read per poll; a gold symbol rarely has more than a few hundred a second


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
                "lot_step": float(sym.volume_step), "max_lot": float(sym.volume_max) or None,
                "point": float(sym.point), "digits": int(sym.digits)}

    def daily_history(self, clock: str, count: int) -> pd.DataFrame | None:
        # A D1 bar runs from the broker's midnight; only on a New York +7 clock is that the
        # 17:00 New York close, so the bar is one trading day.
        if clock != "NY+7":
            return None
        if self._mt5 is None:
            self.reconnect()
        rates = self._mt5.copy_rates_from_pos(self.symbol, self._mt5.TIMEFRAME_D1, 1, count)  # 1: skip today's
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df.index = pd.to_datetime(df["time"], unit="s").normalize()
        return normalize_ohlcv(df.rename(columns={"tick_volume": "volume"}))

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

    def last_tick(self) -> tuple[pd.Timestamp, float] | None:
        tick = self._mt5.symbol_info_tick(self.symbol) if self._mt5 else None
        if not tick or not tick.bid:
            return None
        return pd.Timestamp(int(tick.time), unit="s"), float(tick.bid)  # broker server time, like the bars

    def new_ticks(self) -> list[tuple[pd.Timestamp, float]]:
        """Every tick since the last call (``copy_ticks_from`` after the last ``time_msc`` seen), so
        the forming candle's high and low are exact however slowly it's polled. The first call, or
        a terminal that returns nothing, falls back to the latest tick."""
        mt5 = self._mt5
        if mt5 is None:
            return []
        last = getattr(self, "_last_msc", None)
        if last is not None and hasattr(mt5, "copy_ticks_from"):
            got = mt5.copy_ticks_from(self.symbol, int(last // 1000), TICK_BATCH, mt5.COPY_TICKS_INFO)
            if got is not None and len(got):
                fresh = [t for t in got if int(t["time_msc"]) > last and float(t["bid"]) > 0]
                if fresh:
                    self._last_msc = int(fresh[-1]["time_msc"])
                    return [(pd.Timestamp(int(t["time_msc"]), unit="ms"), float(t["bid"])) for t in fresh]
                return []
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick or not tick.bid:
            return []
        msc = int(getattr(tick, "time_msc", 0) or int(tick.time) * 1000)
        if last is not None and msc <= last:
            return []
        self._last_msc = msc
        return [(pd.Timestamp(msc, unit="ms"), float(tick.bid))]

    def account_summary(self) -> dict:
        """The logged-in account's number, server and company (no history). Runs on the scan thread."""
        mt5 = self._mt5
        if mt5 is None:
            raise RuntimeError("The MT5 terminal isn't connected yet")
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError(f"MT5 has no account logged in: {mt5.last_error()}")
        return {"login": str(acc.login), "server": acc.server, "company": acc.company}

    def account_activity(self) -> dict:
        """A cheap look at the account for the journal's auto-sync: its number, how many deals it has and
        how many positions are open. A change in either means a full sync is worth it. Scan thread only."""
        mt5 = self._mt5
        if mt5 is None:
            raise RuntimeError("The MT5 terminal isn't connected yet")
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError(f"MT5 has no account logged in: {mt5.last_error()}")
        deals = mt5.history_deals_total(datetime(2000, 1, 1), datetime.now() + timedelta(days=3))
        return {"login": str(acc.login), "deals": int(deals or 0), "positions": int(mt5.positions_total() or 0)}

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

    def daily_history(self, clock: str, count: int) -> pd.DataFrame | None:
        # Yahoo dates a futures daily bar by its session, which ends at the 17:00 New York close.
        import yfinance as yf

        df = yf.Ticker(self.symbol).history(period="max", interval="1d", auto_adjust=False)
        if df.empty:
            return None
        df = df.copy()
        df.index = (df.index.tz_convert("America/New_York").tz_localize(None) if df.index.tz is not None
                    else df.index).normalize()
        return normalize_ohlcv(df).iloc[:-1].tail(count)  # the last one may still be forming

    def last_tick(self) -> tuple[pd.Timestamp, float] | None:
        # The forming minute's close. Its bar time also says when the price is from, so a
        # closed market (last bar hours old) doesn't start a candle now.
        df = self._history("1d")
        if df.empty:
            return None
        t = df.index[-1]
        t = t.tz_convert("UTC").tz_localize(None) if t.tz is not None else t
        return t, float(df["Close"].iloc[-1])


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
    """Random-walk gold-like prices for demos and tests. Each fetch advances one minute.

    With ``anchor`` (time, price) from real stored bars, the demo sits at today's price level: the
    walk before that time is shifted to end exactly at that price, and from there on it walks on.
    The candles stay made up; only where they start from is real."""

    name = "synthetic"
    persist = False  # every run is a different random walk; storing it would splice unrelated series

    def __init__(self, start_price: float = 2650.0, seed: int | None = 42,
                 history: int = 60_000, end: pd.Timestamp | None = None,
                 anchor: tuple[pd.Timestamp, float] | None = None):
        self._rng = np.random.default_rng(seed)
        self._forming: dict | None = None  # the next minute, built from ticks
        end = (end or pd.Timestamp.now(tz="UTC").tz_localize(None)).floor("min") - M1
        index = pd.date_range(end=end, periods=history, freq="1min")
        self._bars = self._generate(index, start_price) if anchor is None else self._anchored(index, *anchor)

    def _anchored(self, index: pd.DatetimeIndex, at: pd.Timestamp, price: float) -> pd.DataFrame:
        """A walk that passes through ``price`` at ``at``: shifted to end there, then walking on from it."""
        before, after = index[index <= at], index[index > at]
        parts = []
        if len(before):
            walk = self._generate(before, price)
            parts.append(walk + np.array([price - walk["close"].iloc[-1]] * 4 + [0.0]))  # shift OHLC, not volume
        if len(after):
            parts.append(self._generate(after, price))
        return pd.concat(parts)

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

    def last_tick(self) -> tuple[pd.Timestamp, float]:
        # A small random step within the next minute; its ticks become that minute's bar.
        t = self._bars.index[-1] + M1
        f = self._forming
        price = (f["close"] if f else float(self._bars["close"].iloc[-1])) + float(self._rng.normal(0, 0.15))
        if f is None:
            open_ = float(self._bars["close"].iloc[-1])
            f = self._forming = {"open": open_, "high": open_, "low": open_, "volume": 0.0}
        f["high"], f["low"], f["close"] = max(f["high"], price), min(f["low"], price), price
        f["volume"] += 1
        return t, price

    def fetch_m1(self, count: int) -> pd.DataFrame:
        # Each fetch closes one minute: the one the ticks built, or a generated one without ticks.
        t = self._bars.index[-1] + M1
        if self._forming:
            nxt = pd.DataFrame([self._forming], index=pd.DatetimeIndex([t]))[OHLCV_COLUMNS]
            self._forming = None
        else:
            nxt = self._generate(pd.DatetimeIndex([t]), float(self._bars["close"].iloc[-1]))
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


def build_feed(settings: DataSettings, mt5_password: str | None = None,
               anchor: tuple[pd.Timestamp, float] | None = None) -> DataFeed:
    """``anchor``: a real (time, price) for the demo data to start from (see SyntheticFeed)."""
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
        return SyntheticFeed(anchor=anchor)
    raise ValueError(f"Unknown source {source!r}")
