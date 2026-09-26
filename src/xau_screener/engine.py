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

from .feeds import DataFeed, M1Buffer
from .scanner import ScanConfig, ScanResult, scan

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
    def __init__(self, feed: DataFeed, cfg: ScanConfig, symbol: str):
        self.feed = feed
        self.cfg = cfg
        self.symbol = symbol
        self.buffer = M1Buffer(feed, max_bars=cfg.required_m1_bars())
        self.state = EngineState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def step(self) -> ScanResult:
        """Fetch new M1 bars and rescan. Errors are recorded in the state and re-raised."""
        try:
            m1 = self.buffer.update()
            result = scan(m1, self.cfg, self.feed.last_price())
        except Exception as exc:
            with self.state.lock:
                self.state.error = f"{type(exc).__name__}: {exc}"
                self.state.error_at = datetime.now(timezone.utc)
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
        """Connect, then scan now and after every minute close until :meth:`stop`."""
        self.feed.connect()
        try:
            while not self._stop.is_set():
                try:
                    result = self.step()
                    if on_result:
                        on_result(result)
                except Exception:
                    log.exception("scan failed; retrying next minute")
                self._stop.wait(seconds_to_next_minute(delay))
        finally:
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
