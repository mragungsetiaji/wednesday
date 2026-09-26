"""Scan engine shared by the console loop and the web server.

All feed calls (including MetaTrader 5, which is not thread-safe) happen on the
thread that calls :meth:`Engine.step`; readers only get snapshots.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .feeds import DataFeed, M1Buffer, build_feed
from .scanner import ScanConfig, ScanResult, scan
from .settings import DataSettings
from .storage import Store

log = logging.getLogger(__name__)


def seconds_to_next_minute(delay: float) -> float:
    return 60 - (time.time() % 60) + delay


@dataclass
class EngineState:
    version: int = 0  # bumps on every successful scan
    result: ScanResult | None = None
    m1: pd.DataFrame | None = None
    scanned_at: datetime | None = None
    error: str | None = None
    error_at: datetime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class Engine:
    def __init__(self, feed: DataFeed, cfg: ScanConfig, symbol: str, store: Store | None = None):
        self.feed = feed
        self.cfg = cfg
        self.symbol = symbol
        self.store = store
        key = (feed.name, symbol) if feed.persist else None
        self.buffer = M1Buffer(feed, max_bars=cfg.required_m1_bars(), store=store if key else None, key=key)
        self.state = EngineState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _record_error(self, exc: Exception) -> None:
        with self.state.lock:
            self.state.error = f"{type(exc).__name__}: {exc}"
            self.state.error_at = datetime.now(timezone.utc)

    def step(self) -> ScanResult:
        """Fetch new M1 bars and rescan. Errors are recorded in the state and re-raised."""
        try:
            m1 = self.buffer.update()
            result = scan(m1, self.cfg, self.feed.last_price())
        except Exception as exc:
            self._record_error(exc)
            raise
        with self.state.lock:
            self.state.version += 1
            self.state.result = result
            self.state.m1 = m1
            self.state.scanned_at = datetime.now(timezone.utc)
            self.state.error = None
            self.state.error_at = None
        return result

    def snapshot(self) -> tuple[int, ScanResult | None, pd.DataFrame | None]:
        with self.state.lock:
            return self.state.version, self.state.result, self.state.m1

    def run_forever(self, delay: float = 2.0, on_result=None) -> None:
        """Connect, then scan now and after every minute close until :meth:`stop`.

        A failed connection is recorded like any other error and retried on the
        next minute, so a terminal that is still starting doesn't kill the loop.
        """
        connected = False
        try:
            while not self._stop.is_set():
                try:
                    if not connected:
                        self.feed.connect()
                        connected = True
                    result = self.step()
                    if on_result:
                        on_result(result)
                except Exception as exc:
                    if not connected:
                        self._record_error(exc)
                    log.exception("scan failed; retrying next minute")
                self._stop.wait(seconds_to_next_minute(delay))
        finally:
            if connected:
                self.feed.close()

    def start(self, delay: float = 2.0, on_result=None) -> threading.Thread:
        """Run :meth:`run_forever` on a daemon thread (used by the web server)."""
        self._thread = threading.Thread(target=self.run_forever, args=(delay, on_result),
                                        name="scan-engine", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float | None = 10) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)


class Runtime:
    """Owns the running engine so the dashboard can switch data sources live."""

    def __init__(self, cfg: ScanConfig, settings: DataSettings, store: Store | None = None,
                 mt5_password: str | None = None, delay: float = 2.0, on_result=None):
        self.cfg = cfg
        self.store = store
        self.mt5_password = mt5_password
        self.delay = delay
        self.on_result = on_result
        self._lock = threading.Lock()
        self.settings = settings
        self.engine = self._build(settings)

    def _build(self, settings: DataSettings) -> Engine:
        feed = build_feed(settings, self.mt5_password)
        return Engine(feed, self.cfg, settings.resolved_symbol, self.store)

    def start(self) -> None:
        self.engine.start(self.delay, self.on_result)

    def stop(self) -> None:
        self.engine.stop()

    def apply(self, settings: DataSettings) -> None:
        """Stop the current feed, then start one for ``settings``."""
        with self._lock:
            new = self._build(settings)  # fails fast on a bad config, before stopping anything
            self.engine.stop(timeout=30)
            self.settings = settings
            self.engine = new
            new.start(self.delay, self.on_result)
