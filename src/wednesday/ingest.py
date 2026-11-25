"""The market-data store's ingest: the source's own candles per timeframe, kept in the ``bars``
table and in memory for the scan and the charts (``wed-engine/src/ingest.rs`` does the same).

TradingView caps a series by bar count (about 5 days of 1m), so 4H built from 1m reaches only as
far back as the stored 1m, and its candles open on a midnight grid instead of TradingView's
sessions. Each timeframe is therefore fetched as the source builds it. The engine calls
:meth:`Ingest.refresh` after every minute's M1 update (a timeframe is fetched again only once a
new candle of it has closed) and :meth:`Ingest.backfill` once a minute, which loads one older
page of one series until the source has nothing older. Only closed candles are kept.

A source without its own timeframes (synthetic) has none: the scan resamples M1 as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .feeds import DataFeed
from .storage import Store
from .timeframes import OHLCV_COLUMNS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeTf:
    """A timeframe the source builds itself: the scan's name for it, the ``bars.tf`` it is
    stored under, TradingView's resolution and its length."""
    scan: str
    tf: str
    resolution: str
    minutes: int


# The stored timeframes beside 1m, highest first. 1D isn't scanned; it is kept for the charts.
NATIVE = (
    NativeTf("1D", "1d", "D", 1440),
    NativeTf("4H", "4h", "240", 240),
    NativeTf("1H", "1h", "60", 60),
    NativeTf("30M", "30m", "30", 30),
    NativeTf("15M", "15m", "15", 15),
    NativeTf("5M", "5m", "5", 5),
)
M1 = NativeTf("1M", "1m", "1", 1)  # stored by the engine's M1 buffer, backfilled here

KEEP = 2000  # closed candles kept in memory per timeframe: the scan's lookback, the chart's page
PAGE = 5000  # candles per request; TradingView sends history in chunks of this size
RETRY_MINUTES = 5  # before fetching again after a fetch that failed or brought nothing new


def native_tf(name: str) -> NativeTf | None:
    """The native timeframe a scan or chart timeframe ("4H") reads, if any."""
    return next((n for n in NATIVE if n.scan == name.upper()), None)


def closed(bars: pd.DataFrame, minutes: int, now: pd.Timestamp) -> pd.DataFrame:
    """The closed candles of ``bars`` at ``now``: once a later one exists or its length has passed."""
    if bars.empty:
        return bars
    last_closed = bars.index[-1] + pd.Timedelta(minutes=minutes) <= now
    return bars if last_closed else bars.iloc[:-1]


def merge(older: pd.DataFrame | None, newer: pd.DataFrame, keep: int = KEEP) -> pd.DataFrame:
    """``older`` then ``newer`` by time, a repeated time keeping the newer bar, the last ``keep``."""
    both = newer if older is None or older.empty else pd.concat([older, newer])
    return both[~both.index.duplicated(keep="last")].sort_index().tail(keep)


def forming(native: pd.DataFrame, m1: pd.DataFrame | None, minutes: int) -> pd.DataFrame:
    """The forming candle(s) of a native timeframe from the M1 bars after its last closed candle,
    on its grid (anchored at that candle), so the chart ends at the live price."""
    if native.empty or m1 is None or m1.empty:
        return _empty()
    step = pd.Timedelta(minutes=minutes)
    anchor = native.index[-1] + step
    after = m1[m1.index >= anchor]
    if after.empty:
        return _empty()
    starts = anchor + ((after.index - anchor) // step) * step
    out = after.groupby(starts).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    out.index.name = None
    return out[OHLCV_COLUMNS]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([]), dtype=float)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


class Ingest:
    """The ingest of one (source, symbol): native candles in memory, their series in the store."""

    def __init__(self, source: str, symbol: str, store: Store | None = None):
        self.source = source
        self.symbol = symbol
        self.store = store
        self.native: dict[str, pd.DataFrame] = {}  # closed candles by scan timeframe name, the last KEEP
        self._next_check: dict[str, pd.Timestamp] = {}
        self._backfill: dict[str, str] = {}
        self._started = False
        self._unsupported = False  # the source builds no timeframes of its own (synthetic)

    def refresh(self, feed: DataFeed) -> bool:
        """Load what the store has (once), then fetch every timeframe with a newly closed candle.
        True when a timeframe got new candles. A timeframe that fails is noted in its series row
        and tried again later; the scan resamples M1 for it meanwhile. Never raises."""
        if self._unsupported:
            return False
        now = _now()
        if not self._started:
            self._started = True
            self._load()
        changed = False
        for n in NATIVE:
            if n.scan in self._next_check and now < self._next_check[n.scan]:
                continue
            have = self.native.get(n.scan)
            dur = pd.Timedelta(minutes=n.minutes)
            if have is None or have.empty:
                count = KEEP + 1
            else:
                last = have.index[-1]
                if now < last + 2 * dur and n.scan in self._next_check:
                    continue  # the next candle is still forming
                count = min(int((now - last) / dur) + 3, PAGE)
            try:
                got = feed.fetch_native(n.resolution, count)
            except Exception as exc:  # noqa: BLE001 - one timeframe never stops the scan
                log.warning("%s %s: %s", self.symbol, n.tf, exc)
                self._note(n, str(exc))
                self._next_check[n.scan] = now + pd.Timedelta(minutes=RETRY_MINUTES)
                if getattr(exc, "code", None):
                    continue
                return changed  # a timeout or a dropped connection: the price fetch tells the engine
            if got is None:
                self._unsupported = True
                return False
            fetched = closed(got, n.minutes, now)
            new = len(fetched) > 0 and (have is None or have.empty or fetched.index[-1] > have.index[-1])
            if self.store is not None:
                try:
                    self.store.save_bars(self.source, self.symbol, fetched, tf=n.tf)
                except Exception as exc:  # noqa: BLE001
                    log.warning("storing %s %s: %s", self.symbol, n.tf, exc)
            self.native[n.scan] = merge(have, fetched)
            self._note(n)
            # Nothing new (the market is closed, say): look again in a while, not every minute.
            wait = pd.Timedelta(0) if new else pd.Timedelta(minutes=min(RETRY_MINUTES, n.minutes))
            self._next_check[n.scan] = now + wait
            changed |= new
        return changed

    def _load(self) -> None:
        """Native candles in memory from the store, and each series' backfill state."""
        if self.store is None:
            return
        for n in NATIVE:
            try:
                bars = self.store.load_bars(self.source, self.symbol, KEEP, tf=n.tf)
            except Exception as exc:  # noqa: BLE001
                log.warning("loading %s %s: %s", self.symbol, n.tf, exc)
                continue
            if not bars.empty:
                self.native[n.scan] = bars
        states = {r["tf"]: r["backfill"] for r in self.store.series(self.source, self.symbol)}
        for n in (*NATIVE, M1):
            self._backfill[n.tf] = states.get(n.tf) or "pending"

    def backfill(self, feed: DataFeed) -> bool:
        """One page of older history for the first series still being backfilled; True when it
        did something. Highest timeframes first: they reach furthest back for the fewest bars."""
        if self.store is None or self._unsupported or not self._started:
            return False
        n = next((n for n in (*NATIVE, M1) if self._backfill.get(n.tf) != "done"), None)
        if n is None:
            return False
        _, first, _ = self.store.bar_bounds(self.source, self.symbol, n.tf)
        if first is None:
            return False  # nothing stored yet: refresh comes first
        try:
            page = feed.fetch_native(n.resolution, PAGE, to=int(first))
        except Exception as exc:  # noqa: BLE001
            # TradingView refusing older history ends the backfill; a timeout tries again.
            log.warning("backfilling %s %s: %s", self.symbol, n.tf, exc)
            if getattr(exc, "code", None):
                self._backfill[n.tf] = "done"
            self._note(n, str(exc))
            return True
        if page is None:
            return False
        older = page[page.index < pd.Timestamp(int(first), unit="s")]
        state = "done" if older.empty else "running"
        self.store.save_bars(self.source, self.symbol, older, tf=n.tf)
        log.info("%s %s: %d older candles%s", self.symbol, n.tf, len(older),
                 ", the source has nothing older" if state == "done" else "")
        self._backfill[n.tf] = state
        self._note(n)
        return True

    def stopped(self) -> None:
        """The engine stopped: its series are no longer kept up to date."""
        if self.store is not None:
            self.store.deactivate_series(self.source, self.symbol)

    def _note(self, n: NativeTf, error: str | None = None) -> None:
        if self.store is None:
            return
        try:
            self.store.note_series(self.source, self.symbol, n.tf, self._backfill.get(n.tf, "pending"), error)
        except Exception as exc:  # noqa: BLE001
            log.warning("series %s %s: %s", self.symbol, n.tf, exc)
