"""Keep journals in sync with the MT5 terminal without a button (issue #42).

After each scan :meth:`AutoSync.check` looks at the account cheaply
(deal count and open positions, ``MT5Feed.account_activity``) and runs the full sync only
when something changed, when the feed (re)connected, or every few minutes while positions
are open so the floating result stays current. The two MT5 reads go through the feed
thread (``Engine.call``); working out the journal happens on the caller's thread. A terminal on another account is waited
for, not an error. Failures keep the last good data and retry with a growing pause.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .service import JournalError, Journals

log = logging.getLogger(__name__)

OPEN_REFRESH = 300  # seconds between syncs while positions are open, for the floating result
MAX_BACKOFF = 1800  # longest pause after failures in a row


@dataclass
class _Seen:
    key: tuple[int, int] | None = None  # (deals, open positions) at the last full sync
    synced: float = 0.0  # monotonic time of the last full sync
    failures: int = 0
    retry_at: float = 0.0


@dataclass
class AutoSync:
    journals: Journals
    clock: object = time.monotonic  # replaced in tests
    _seen: dict[str, _Seen] = field(default_factory=dict)
    _feed: object = None

    def _set(self, journal_id: str, state: str, error: str | None = None) -> None:
        self.journals.status[journal_id] = {"state": state, "error": error}

    def check(self, feed, call=None) -> None:
        """Sync the journals that want it; never raises. ``call(fn)`` runs ``fn(feed)`` on the feed's
        thread (``Engine.call``); without it the feed is used directly (tests, the feed thread itself)."""
        self._call = call or (lambda fn: fn(feed))
        wanted = [j for j in self.journals.store.journals() if not j.get("sample") and Journals.options(j)["auto_sync"]]
        if not wanted:
            return
        if feed is not self._feed:  # a new or restarted feed: sync once, whatever the counts say
            self._feed, self._seen = feed, {}
        if not hasattr(feed, "account_activity"):
            for j in wanted:
                self._set(j["id"], "waiting", "The data source isn't MT5. Switch it in Settings > Data source")
            return
        now = self.clock()
        try:
            activity = self._call(lambda f: f.account_activity())
        except Exception as exc:  # noqa: BLE001 - the terminal not being there is a status, not a crash
            for j in wanted:
                self._set(j["id"], "error", str(exc))
            return
        for j in wanted:
            self._check_one(feed, j, activity, now)

    def _check_one(self, feed, j: dict, activity: dict, now: float) -> None:
        jid = j["id"]
        if j.get("login") and str(j["login"]) != activity["login"]:
            self._set(jid, "waiting", f"Waiting for account {j['login']} in the terminal")
            return
        if not j.get("login"):
            self._set(jid, "waiting", "Add the MT5 account number in the journal settings")
            return
        seen = self._seen.setdefault(jid, _Seen())
        if now < seen.retry_at:
            return
        key = (activity["deals"], activity["positions"])
        stale = activity["positions"] > 0 and now - seen.synced >= OPEN_REFRESH
        if seen.key == key and not stale:
            return
        self._set(jid, "syncing")
        try:
            history = self._call(lambda f: f.account_history())
            self.journals.sync(jid, history)
        except (JournalError, RuntimeError, TimeoutError) as exc:
            seen.failures += 1
            seen.retry_at = now + min(60 * 2 ** (seen.failures - 1), MAX_BACKOFF)
            self._set(jid, "error", str(exc))
            log.warning("journal auto-sync of %s failed (%s); retrying later", j["name"], exc)
            return
        seen.key, seen.synced, seen.failures, seen.retry_at = key, now, 0, 0.0
