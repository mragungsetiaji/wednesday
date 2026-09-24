"""1-minute data feeds.

Every feed returns *closed* M1 bars as an OHLCV DataFrame indexed by bar open
time. Add a new broker/API by subclassing :class:`DataFeed`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
import pandas as pd

from .timeframes import M1, normalize_ohlcv


class DataFeed(ABC):
    name = "base"

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


class MT5Feed(DataFeed):
    """MetaTrader 5 terminal (Windows, ``uv sync --extra mt5``).

    Bar times are broker server time, so resampled 4H/1H candles line up with
    the MT5 chart.
    """

    name = "mt5"

    def __init__(self, symbol: str = "XAUUSD", login: int | None = None,
                 password: str | None = None, server: str | None = None, path: str | None = None):
        self.symbol = symbol
        self.login, self.password, self.server, self.path = login, password, server, path
        self._mt5 = None

    def connect(self) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:  # pragma: no cover - platform specific
            raise RuntimeError("MetaTrader5 is not installed. Run: uv sync --extra mt5 (Windows only)") from exc
        kwargs = {k: v for k, v in {"login": self.login, "password": self.password,
                                    "server": self.server, "path": self.path}.items() if v is not None}
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"MT5 symbol {self.symbol!r} not available: {mt5.last_error()}")
        self._mt5 = mt5

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    def fetch_m1(self, count: int) -> pd.DataFrame:
        mt5 = self._mt5
        if mt5 is None:
            raise RuntimeError("MT5Feed.connect() was not called")
        # Position 0 is the still-forming bar; start at 1 for closed bars only.
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 1, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 returned no M1 data: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df.index = pd.to_datetime(df["time"], unit="s")
        df = df.rename(columns={"tick_volume": "volume"})
        return normalize_ohlcv(df)

    def last_price(self) -> float | None:
        tick = self._mt5.symbol_info_tick(self.symbol) if self._mt5 else None
        return float(tick.bid) if tick else None


class YFinanceFeed(DataFeed):
    """Yahoo Finance (``uv sync --extra yfinance``). Handy for testing off-MT5.

    Note: Yahoo only serves ~7 days of 1m data and ``GC=F`` is COMEX gold
    futures, not spot XAUUSD, so levels differ from a broker chart.
    """

    name = "yfinance"

    def __init__(self, symbol: str = "GC=F"):
        self.symbol = symbol

    def fetch_m1(self, count: int) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinance is not installed. Run: uv sync --extra yfinance") from exc
        df = yf.Ticker(self.symbol).history(period="7d", interval="1m", auto_adjust=False)
        if df.empty:
            raise RuntimeError(f"yfinance returned no data for {self.symbol}")
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

    def __init__(self, feed: DataFeed, max_bars: int, update_bars: int = 30):
        self.feed = feed
        self.max_bars = max_bars
        self.update_bars = update_bars
        self.bars: pd.DataFrame | None = None

    def update(self) -> pd.DataFrame:
        if self.bars is None or self.bars.empty:
            self.bars = normalize_ohlcv(self.feed.fetch_m1(self.max_bars))
        else:
            fresh = normalize_ohlcv(self.feed.fetch_m1(self.update_bars))
            if not fresh.empty and fresh.index[0] > self.bars.index[-1] + M1:
                # Gap bigger than the update window (e.g. reconnect): reload everything.
                fresh = normalize_ohlcv(self.feed.fetch_m1(self.max_bars))
            merged = pd.concat([self.bars, fresh])
            self.bars = merged[~merged.index.duplicated(keep="last")].sort_index()
        self.bars = self.bars.tail(self.max_bars)
        return self.bars


def build_feed(source: str, symbol: str | None = None, csv_path: str | None = None, **mt5_kwargs) -> DataFeed:
    source = source.lower()
    if source == "mt5":
        return MT5Feed(symbol or "XAUUSD", **mt5_kwargs)
    if source == "yfinance":
        return YFinanceFeed(symbol or "GC=F")
    if source == "csv":
        if not csv_path:
            raise ValueError("--csv is required with --source csv")
        return CSVFeed(csv_path)
    if source == "synthetic":
        return SyntheticFeed()
    raise ValueError(f"Unknown source {source!r}")
