"""Scan engine shared by the console loop and the web server.

All feed calls (including MetaTrader 5, which is not thread-safe) happen on the
thread that calls :meth:`Engine.step`; readers only get snapshots.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .llm_usage import UsageLog
from .bias import BIAS_KEY, TradeBias, active
from .drawing_alerts import DrawingAlerts
from .feeds import DataFeed, M1Buffer, build_feed
from .plugins import Hooks
from .scanner import ScanConfig, ScanResult, scan
from . import secret_store
from .settings import MT5_SERVICE, DataSettings, mt5_account
from .sizing import RISK_KEY, RiskSettings, Sizer, resolve
from .storage import Store

log = logging.getLogger(__name__)


# A feed that doesn't retry its connection (MT5) gets this many tries after losing it.
RECONNECT_ATTEMPTS = 3
# A tick starts the forming candle only this soon after the last closed bar: older bars mean
# the market is closed or the feed lags, and a candle "now" would sit after a gap.
FORMING_WINDOW = pd.Timedelta(minutes=5)


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
    # connecting -> connected; reconnecting while a lost connection is retried; failed once it gave up.
    conn: str = "connecting"
    attempt: int = 0  # reconnect attempts so far
    spec: dict | None = None  # balance and contract spec from the feed (MT5), for position sizing
    # Between scans: {"price", "time", "bar"}; bar is the forming M1 candle built from ticks, or None.
    live: dict | None = None
    tick: int = 0  # bumps on every live price
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class Engine:
    def __init__(self, feed: DataFeed, cfg: ScanConfig, symbol: str, store: Store | None = None,
                 tick_seconds: float = 0, poll: bool = True):
        self.feed = feed
        self.poll = poll  # False: scan once, then no minute scans and no live prices (dev: a still chart)
        self.tick_seconds = tick_seconds if poll else 0  # live price interval between scans; 0 (or a minute) = none
        self.cfg = cfg
        self.symbol = symbol
        self.store = store
        key = (feed.name, symbol) if feed.persist else None
        self.buffer = M1Buffer(feed, max_bars=cfg.required_m1_bars(), store=store if key else None, key=key)
        self.state = EngineState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tasks: queue.Queue = queue.Queue()  # work that must run on the feed's thread (see call)
        self.on_tick = None  # called with (price, time) after each live price, outside the state lock

    def _record_error(self, exc: Exception) -> None:
        with self.state.lock:
            self.state.error = f"{type(exc).__name__}: {exc}"
            self.state.error_at = datetime.now(timezone.utc)

    def _set_conn(self, conn: str, attempt: int = 0) -> None:
        with self.state.lock:
            self.state.conn = conn
            self.state.attempt = attempt

    def step(self) -> ScanResult:
        """Fetch new M1 bars and rescan. Errors are recorded in the state and re-raised."""
        try:
            m1 = self.buffer.update()
            result = scan(m1, self.cfg, self.feed.last_price())
        except Exception as exc:
            self._record_error(exc)
            raise
        try:
            spec = self.feed.trading_spec()
        except Exception:  # sizing falls back to Settings > Risk; never fail a scan over it
            log.debug("no trading spec", exc_info=True)
            spec = None
        with self.state.lock:
            self.state.version += 1
            self.state.result = result
            self.state.m1 = m1
            self.state.spec = spec
            self.state.scanned_at = datetime.now(timezone.utc)
            self.state.error = None
            self.state.error_at = None
            live = self.state.live
            if live and live["bar"] is not None and len(m1) and live["bar"]["time"] <= m1.index[-1]:
                live["bar"] = None  # that minute closed: the fetched bar replaces the one ticks built
        return result

    def tick(self) -> None:
        """Poll the live price and fold it into the forming M1 candle. Never raises."""
        try:
            got = self.feed.last_tick()
        except Exception:  # a missed tick is fine; the next scan still runs
            log.debug("tick failed", exc_info=True)
            return
        if got is None:
            return
        at, price = got
        minute = at.floor("min")
        with self.state.lock:
            m1 = self.state.m1
            last = m1.index[-1] if m1 is not None and len(m1) else None
            live = self.state.live or {}
            bar = live.get("bar")
            if last is None or not (last < minute <= last + FORMING_WINDOW):
                bar = None
            elif bar is not None and bar["time"] == minute:
                bar = {**bar, "high": max(bar["high"], price), "low": min(bar["low"], price), "close": price}
            else:
                bar = {"time": minute, "open": price, "high": price, "low": price, "close": price}
            self.state.live = {"price": price, "time": at, "bar": bar}
            self.state.tick += 1
        if self.on_tick is not None:
            try:
                self.on_tick(price, at)
            except Exception:  # noqa: BLE001 - a tick listener must never stop the feed
                log.exception("tick listener failed")

    def snapshot(self) -> tuple[int, ScanResult | None, pd.DataFrame | None]:
        with self.state.lock:
            return self.state.version, self.state.result, self.state.m1

    def call(self, fn, timeout: float = 60):
        """Run ``fn(feed)`` on the scan thread between scans and return its result.

        MT5 must only be used from the thread that connected it, so anything else
        that needs the terminal (the journal's sync) goes through here.
        """
        if self._thread is None or not self._thread.is_alive():
            if self.state.conn == "failed":
                raise RuntimeError(f"Not connected ({self.state.error}). Press Reconnect in Settings.")
            raise RuntimeError("The scanner isn't running")
        done, box = threading.Event(), {}

        def task():
            try:
                box["value"] = fn(self.feed)
            except Exception as exc:  # noqa: BLE001 - handed back to the caller
                box["error"] = exc
            finally:
                done.set()

        self._tasks.put(task)
        if not done.wait(timeout):
            raise TimeoutError("the scan thread didn't run the task in time")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _idle(self, seconds: float) -> None:
        """Wait until the next scan, running queued tasks meanwhile."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                task = self._tasks.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            task()

    def _wait(self, seconds: float) -> None:
        """Wait until the next scan, polling the live price every ``tick_seconds`` meanwhile."""
        if not 0 < self.tick_seconds < 60:
            self._idle(seconds)
            return
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._idle(min(self.tick_seconds, remaining))
            if deadline - time.monotonic() > 0.2:  # not right before the scan, which prices it anyway
                self.tick()

    def run_forever(self, delay: float = 2.0, on_result=None) -> None:
        """Connect, then scan now and after every minute close until :meth:`stop`.

        Most feeds retry a failed connection or scan on the next minute, so a
        network blip doesn't kill the loop. A feed with ``retry_connect = False``
        (MT5) gives up at once when the first connection fails, and after
        ``RECONNECT_ATTEMPTS`` failed minutes in a row once it had connected; the
        state then says ``failed`` until the dashboard restarts the feed.
        """
        connected = False  # the feed connected at least once
        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    if not connected:
                        self.feed.connect()
                        connected = True
                    result = self.step()
                    failures = 0
                    self._set_conn("connected")
                    if on_result:
                        on_result(result)
                    if not self.poll:
                        log.info("%s: polling off, keeping this scan", self.feed.name)
                        while not self._stop.is_set():
                            self._idle(3600)  # still runs queued feed work (scrolling back, journal sync)
                        break
                except Exception as exc:
                    failures += 1
                    self._record_error(exc)
                    if not self.feed.retry_connect and (not connected or failures >= RECONNECT_ATTEMPTS):
                        self._set_conn("failed", failures)
                        log.error("%s: giving up (%s); restart the feed from Settings", self.feed.name, exc)
                        return
                    self._set_conn("reconnecting" if connected else "connecting", failures)
                    log.exception("scan failed; retrying next minute")
                self._wait(seconds_to_next_minute(delay))
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


def load_risk(store: Store | None) -> RiskSettings:
    """Saved Settings > Risk; the defaults (sizing off) when none or unreadable."""
    try:
        return RiskSettings.from_dict(store.get_setting(RISK_KEY) if store else None)
    except (TypeError, ValueError):
        log.warning("ignoring invalid risk settings")
        return RiskSettings()


class Runtime:
    """Owns the running engine so the dashboard can switch data sources live."""

    def __init__(self, cfg: ScanConfig, settings: DataSettings, store: Store | None = None,
                 mt5_password: str | None = None, delay: float = 2.0, on_result=None, alerts=None, brief=None,
                 calendar=None, lab=None, journals=None, poll: bool = True):
        self.cfg = cfg
        self.poll = poll  # False: every engine scans once and stops (--no-poll)
        self.store = store
        self.mt5_password = mt5_password  # MT5_PASSWORD from the environment, for setups that still use it
        self.delay = delay
        self.on_result = on_result
        self.alerts = alerts  # AlertManager or None
        self.drawing_alerts = DrawingAlerts(store) if store else None  # alerts on the trader's drawings
        self.brief = brief  # BriefRunner or None
        # LLM usage log and budget, shared by the brief and plugins' LLM features.
        self.usage = brief.usage if brief is not None else UsageLog(store)
        self.calendar = calendar  # news.Calendar or None
        self.lab = lab  # lab.service.Lab or None (needs a database)
        self.journals = journals  # journal.service.Journals or None (needs a database)
        self.hooks = Hooks()  # filled by plugins (see plugins.py)
        self.bias = TradeBias.from_dict(store.get_setting(BIAS_KEY)) if store else None
        self.risk = load_risk(store)
        self._lock = threading.Lock()
        self.settings = settings
        self.engine = self._build(settings)

    def _build(self, settings: DataSettings) -> Engine:
        feed = build_feed(settings, self.mt5_password_for(settings)[0])
        engine = Engine(feed, self.cfg, settings.resolved_symbol, self.store, tick_seconds=settings.resolved_tick,
                        poll=self.poll)
        engine.on_tick = lambda price, at: self._on_tick(engine, price, at)
        return engine

    def mt5_password_for(self, settings: DataSettings) -> tuple[str | None, str | None]:
        """The MT5 password for the settings' account and where it came from: saved, session or env."""
        if settings.mt5_login is not None:
            pw, source = secret_store.recall(MT5_SERVICE, mt5_account(settings.mt5_login, settings.mt5_server))
            if pw:
                return pw, source
        if self.mt5_password:
            return self.mt5_password, "env"
        return None, None

    def set_mt5_password(self, login: int, server: str | None, password: str) -> None:
        secret_store.remember(MT5_SERVICE, mt5_account(login, server), password)

    def forget_mt5_password(self, login: int | None, server: str | None) -> None:
        secret_store.forget(MT5_SERVICE, mt5_account(login, server))

    def active_bias(self) -> TradeBias | None:
        """The trader's bias, unless it has expired."""
        return active(self.bias)

    def set_bias(self, bias: TradeBias | None) -> None:
        self.bias = bias
        if self.store:
            if bias:
                self.store.set_setting(BIAS_KEY, bias.to_dict())
            else:
                self.store.delete_setting(BIAS_KEY)

    def sizer(self) -> Sizer | None:
        """Position sizing from Settings > Risk and the terminal's balance and spec; None when off."""
        with self.engine.state.lock:
            spec = self.engine.state.spec
        return resolve(self.risk, spec)

    def set_risk(self, risk: RiskSettings) -> None:
        self.risk = risk
        if self.store:
            self.store.set_setting(RISK_KEY, risk.to_dict())

    def _after_scan(self, result) -> None:
        engine = self.engine
        if self.alerts is not None:
            try:
                sent = self.alerts.check(self.settings.source, engine.symbol, result, engine.state.m1, self.active_bias(),
                                         sizer=self.sizer())
            except Exception:  # an alert problem must never stop scanning
                log.exception("alert check failed")
            else:
                self.hooks.run_on_alert(sent, result)
        if self.drawing_alerts is not None:
            try:
                self.drawing_alerts.check(self.settings.source, engine.symbol, engine.state.m1, self.telegram_send())
            except Exception:  # noqa: BLE001 - never stop scanning over an alert
                log.exception("drawing alert check failed")
        self.hooks.run_after_scan(result, engine)
        if self.on_result:
            self.on_result(result)

    def telegram_send(self):
        """Telegram's send when alerts are set up and on; None otherwise (drawing alerts are then only logged)."""
        a = self.alerts
        return a.client.send if a is not None and a.configured and a.settings.enabled else None

    def _on_tick(self, engine: Engine, price: float, at) -> None:
        if self.drawing_alerts is not None and engine is self.engine:
            self.drawing_alerts.check_tick(self.settings.source, engine.symbol, price, at, self.telegram_send())

    def start(self) -> None:
        self.engine.start(self.delay, self._after_scan)

    def run_forever(self) -> None:
        """Console mode: scan in the calling thread."""
        self.engine.run_forever(self.delay, self._after_scan)

    def stop(self) -> None:
        self.hooks.shutdown()
        self.engine.stop()

    def reconnect(self) -> None:
        """Restart the feed with the current settings (after it gave up, or to pick up a new password)."""
        self.apply(self.settings)

    def apply(self, settings: DataSettings) -> None:
        """Stop the current feed, then start one for ``settings``."""
        with self._lock:
            new = self._build(settings)  # fails fast on a bad config, before stopping anything
            self.engine.stop(timeout=30)
            self.settings = settings
            self.engine = new
            new.start(self.delay, self._after_scan)
