"""1-minute data feeds.

Every feed returns *closed* M1 bars as an OHLCV DataFrame indexed by bar open
time. Add a new broker/API by subclassing :class:`DataFeed`.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from .timeframes import M1, OHLCV_COLUMNS, normalize_ohlcv

if TYPE_CHECKING:
    from .settings import DataSettings
    from .storage import Store

log = logging.getLogger(__name__)

class DataFeed(ABC):
    name = "base"
    persist = True  # store fetched bars in the database
    # Keep retrying a failed connection every minute (False: give up after a few tries).
    retry_connect = True

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

    def daily_history(self, clock: str, count: int) -> pd.DataFrame | None:
        """Up to ``count`` closed daily bars indexed by New York trading day (the day a
        17:00 New York close ends), for the move distribution's long lookbacks. None when
        the feed has none, or its days don't end at the New York close."""
        return None

    def describe(self) -> dict:
        """Connection details shown by ``--check``."""
        return {"feed": self.name}


TRADINGVIEW_DIR = Path(__file__).resolve().parents[2] / "tradingview"  # the Node helper, beside src/


def _bundled() -> tuple[Path, str | None]:
    """The helper's folder and node.exe: the Windows app ships both (packaging/wednesday.spec)."""
    base = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and base:
        node = Path(base) / "node" / ("node.exe" if sys.platform == "win32" else "node")
        return Path(base) / "tradingview", str(node) if node.is_file() else None
    return TRADINGVIEW_DIR, None


def tradingview_command() -> tuple[list[str] | None, str | None]:
    """The command that starts the TradingView helper, or why it can't run here."""
    folder, bundled_node = _bundled()
    script = folder / "feed.mjs"
    if not script.is_file():
        return None, "The TradingView helper is missing from this install. Reinstall Wednesday, or run it from source."
    node = bundled_node or shutil.which("node")
    if node is None:
        return None, "Needs Node 20+ (nodejs.org), then: npm install --prefix tradingview"
    if not (folder / "node_modules" / "@mathieuc" / "tradingview").is_dir():
        return None, "Install it with: npm install --prefix tradingview (or make install)"
    return [node, str(script)], None


class TradingViewError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"TradingView: {message}")
        self.code = code


class _NodeHelper:
    """A long-running helper process answering JSON-line requests (see tradingview/feed.mjs).

    Started on the first call and again after it dies. Replies carry the request id, so a late
    reply to a request that already timed out is skipped instead of answering the next one.
    """

    def __init__(self, command: list[str]):
        self.command = command
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()
        self._id = 0
        self._stderr: list[str] = []  # last lines, for the error when the helper stops

    def _start(self) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window from the desktop app
        self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                                      creationflags=flags)
        self._replies = replies = queue.Queue()
        threading.Thread(target=self._read, args=(self._proc.stdout, replies), daemon=True,
                         name="tradingview-out").start()
        threading.Thread(target=self._log, args=(self._proc.stderr,), daemon=True,
                         name="tradingview-err").start()

    @staticmethod
    def _read(stream, replies: queue.Queue) -> None:
        for line in stream:
            try:
                replies.put(json.loads(line))
            except ValueError:
                log.warning("tradingview helper: %s", line.rstrip())
        replies.put(None)  # the helper exited

    def _log(self, stream) -> None:
        for line in stream:
            line = line.rstrip()
            if line:
                self._stderr = (self._stderr + [line])[-5:]
                log.info("tradingview helper: %s", line)

    def call(self, op: str, timeout: float, **args):
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        self._id += 1
        rid = self._id
        try:
            self._proc.stdin.write(json.dumps({"id": rid, "op": op, **args}) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            raise RuntimeError(f"The TradingView helper stopped: {self._why()}") from exc
        deadline = time.monotonic() + timeout
        while True:
            try:
                msg = self._replies.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"TradingView didn't answer within {timeout:.0f} s. "
                                   "Check the internet connection.") from None
            if msg is None:
                raise RuntimeError(f"The TradingView helper stopped: {self._why()}")
            if msg.get("id") != rid:
                continue  # a late reply to an earlier request
            if "error" in msg:
                err = msg["error"] or {}
                raise TradingViewError(err.get("code") or "ERROR", err.get("message") or "unknown error")
            return msg.get("result")

    def _why(self) -> str:
        return self._stderr[-1] if self._stderr else "no output. Is Node 20+ installed?"

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # the helper exits when stdin closes
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


class TradingViewFeed(DataFeed):
    """TradingView market data through a Node helper (``tradingview/``, unofficial library).

    ``FX:XAUUSD`` is FXCM's spot gold; any TradingView symbol with its exchange prefix works
    (``OANDA:XAUUSD``, ...). Bar times are UTC. Without a TradingView account the server gives
    about 7,000 M1 bars (~5 trading days); stored history grows from there, as with Yahoo.
    """

    name = "tradingview"

    def __init__(self, symbol: str = "FX:XAUUSD", command: list[str] | None = None):
        self.symbol = symbol
        self.command = command
        self._helper: _NodeHelper | None = None
        self._info: dict | None = None

    def connect(self) -> None:
        command = self.command
        if command is None:
            command, why = tradingview_command()
            if command is None:
                raise RuntimeError(f"TradingView source unavailable. {why}")
        self.close()
        self._helper = _NodeHelper(command)
        try:
            self._info = self._call("info", 30)
        except TradingViewError as exc:
            if exc.code == "SYMBOL_ERROR":
                raise RuntimeError(f"TradingView has no symbol {self.symbol!r}. Use the exchange prefix, "
                                   "e.g. FX:XAUUSD (FXCM) or OANDA:XAUUSD.") from exc
            raise

    def close(self) -> None:
        if self._helper is not None:
            self._helper.close()
            self._helper = None

    def _call(self, op: str, timeout: float, **args):
        if self._helper is None:
            self.connect()
        return self._helper.call(op, timeout, symbol=self.symbol, **args)

    def _candles(self, timeframe: str, count: int) -> pd.DataFrame:
        # Deep loads come in batches of a few thousand bars; give them time.
        timeout = 30 + count / 200
        rows = self._call("candles", timeout, timeframe=timeframe, count=count, timeoutMs=int(timeout * 1000) - 5000)
        df = pd.DataFrame(rows or [], columns=["time", *OHLCV_COLUMNS])
        df.index = pd.to_datetime(df.pop("time"), unit="s")  # UTC
        return df

    def fetch_m1(self, count: int) -> pd.DataFrame:
        df = self._candles("1", count + 1)  # +1: the forming minute is dropped below
        now = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("min")
        df = df[df.index < now]
        if df.empty:
            raise RuntimeError(f"TradingView returned no 1m data for {self.symbol}")
        return normalize_ohlcv(df).tail(count)

    def daily_history(self, clock: str, count: int) -> pd.DataFrame | None:
        # A gold daily bar opens at 18:00 New York and closes at 17:00 the next day: +7 hours
        # puts its open on the trading day it ends. Indexed by that day, whatever the clock.
        df = self._candles("D", count + 1)
        if df.empty:
            return None
        day = pd.Timedelta(hours=7)
        df.index = (df.index.tz_localize("UTC").tz_convert("America/New_York").tz_localize(None) + day).normalize()
        today = (pd.Timestamp.now(tz="America/New_York").tz_localize(None) + day).normalize()
        return normalize_ohlcv(df[df.index < today]).tail(count)  # today's may still be forming

    def last_tick(self) -> tuple[pd.Timestamp, float] | None:
        q = self._call("quote", 15)
        if not q or q.get("price") is None:
            return None
        t = pd.Timestamp(int(q["time"]), unit="s") if q.get("time") else pd.Timestamp.now(tz="UTC").tz_localize(None)
        return t, float(q["price"])

    def last_price(self) -> float | None:
        tick = self.last_tick()
        return tick[1] if tick else None

    def describe(self) -> dict:
        info = self._info or {}
        tick = self.last_tick()
        return {"feed": self.name, "symbol": self.symbol, "name": info.get("description"),
                "exchange": info.get("exchange"), "last": f"{tick[1]} at {tick[0]} UTC" if tick else None}


class SyntheticFeed(DataFeed):
    """Random-walk gold-like prices for development and tests (``--source synthetic``), not offered
    in the dashboard. Each fetch advances one minute."""

    name = "synthetic"
    persist = False  # every run is a different random walk; storing it would splice unrelated series

    def __init__(self, start_price: float = 2650.0, seed: int | None = 42,
                 history: int = 60_000, end: pd.Timestamp | None = None):
        self._rng = np.random.default_rng(seed)
        self._forming: dict | None = None  # the next minute, built from ticks
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


def build_feed(settings: DataSettings) -> DataFeed:
    source, symbol = settings.source, settings.resolved_symbol
    if source == "tradingview":
        return TradingViewFeed(symbol)
    if source == "synthetic":
        return SyntheticFeed()
    raise ValueError(f"Unknown source {source!r}")
