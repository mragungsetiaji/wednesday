"""Markets kept live (Settings > Markets, ``keep_live``; ``keeper.rs``): their M1 bars and
their candles per timeframe are stored even while another market is scanned, so switching to
one finds its history without a gap. Nothing about them is scanned.

Once a minute the keeper reads the list, opens a TradingView connection per kept market
(closing the ones no longer kept), stores the new M1 bars and runs the market's ``Ingest``
(refresh, then one page of backfill), as the engine does for the scanned one. The scanned market
is left to the engine. A failure is logged and retried the next minute; it never stops the
engine.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import pandas as pd

from .feeds import TradingViewFeed
from .ingest import Ingest
from .storage import Store

log = logging.getLogger(__name__)

UPDATE_BARS = 30  # as the engine's M1 update
FIRST_BARS = 5000  # the first fetch of a market: as many as a scan reads


class Kept:
    def __init__(self, symbol: str, store: Store, feed: TradingViewFeed | None = None):
        self.symbol = symbol
        self.store = store
        self.feed = feed or TradingViewFeed(symbol)
        self.ingest = Ingest("tradingview", symbol, store)
        self.connected = False
        self.last: pd.Timestamp | None = None

    def step(self) -> None:
        if not self.connected:
            self.feed.connect()
            self.connected = True
        if self.last is None:
            stored = self.store.last_bar("tradingview", self.symbol)
            self.last = pd.Timestamp(stored[0], unit="s") if stored else None
        fresh = self.feed.fetch_m1(UPDATE_BARS if self.last is not None else FIRST_BARS)
        if self.last is not None and len(fresh) and fresh.index[0] > self.last + pd.Timedelta(minutes=1):
            fresh = self.feed.fetch_m1(FIRST_BARS)  # a gap: all the feed has
        if self.last is not None:
            fresh = fresh[fresh.index > self.last - pd.Timedelta(minutes=5)]
        if len(fresh):
            self.store.save_bars("tradingview", self.symbol, fresh)
            self.last = fresh.index[-1]
        self.ingest.refresh(self.feed)
        self.ingest.backfill(self.feed)

    def close(self) -> None:
        try:
            self.feed.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            log.debug("closing %s failed", self.symbol, exc_info=True)
        self.connected = False


class Keeper:
    """Keeps the ``keep_live`` markets beside the scanned one. ``scanned`` gives the running
    source and symbol; ``feed`` makes a market's feed (tests)."""

    def __init__(self, store: Store, scanned: Callable[[], tuple[str, str]],
                 feed: Callable[[str], TradingViewFeed] | None = None):
        self.store = store
        self.scanned = scanned
        self.feed = feed
        self.kept: dict[str, Kept] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def round(self) -> None:
        """One minute's work: follow the list, then keep each market."""
        source, scanned = self.scanned()
        if source != "tradingview":
            wanted: list[str] = []  # synthetic runs are tests: nothing to keep
        else:
            try:
                wanted = [m["symbol"] for m in self.store.markets() if m["keep_live"]]
            except Exception:  # noqa: BLE001 - try again next minute
                log.warning("keep live: reading the markets failed", exc_info=True)
                return
        for symbol in [s for s in self.kept if s not in wanted or s == scanned]:
            k = self.kept.pop(symbol)
            if symbol not in wanted:
                k.ingest.stopped()  # the engine marks its own series active when it scans this one
            k.close()
        for symbol in wanted:
            if symbol == scanned:
                continue
            k = self.kept.get(symbol)
            if k is None:
                log.info("keep live: %s", symbol)
                k = self.kept[symbol] = Kept(symbol, self.store, self.feed(symbol) if self.feed else None)
            try:
                k.step()
            except Exception as exc:  # noqa: BLE001 - a market that fails is retried next minute
                log.warning("keep live %s: %s; trying again next minute", symbol, exc)
                k.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.round()
            self._stop.wait(60)
        for k in self.kept.values():
            k.close()
        self.kept.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="keep-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
