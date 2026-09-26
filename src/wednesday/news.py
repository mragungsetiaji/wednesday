"""Economic calendar: upcoming high-impact news, for the risk-time warning.

The default source is ForexFactory's free weekly JSON export (this week's
events with country, impact, forecast and previous). It is fetched at most once
an hour on a background thread; the last good copy is kept in the store so a
restart doesn't refetch and a failed fetch keeps showing what was known.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .storage import Store

log = logging.getLogger(__name__)

CALENDAR_KEY = "calendar"
CALENDAR_CACHE_KEY = "calendar_cache"
DEFAULT_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACTS = ("High", "Medium", "Low")
REFRESH = timedelta(hours=1)
RETRY = timedelta(minutes=10)


@dataclass
class CalendarSettings:
    enabled: bool = True
    url: str = DEFAULT_URL
    currencies: list[str] = field(default_factory=lambda: ["USD"])
    impacts: list[str] = field(default_factory=lambda: ["High"])

    def validate(self) -> list[str]:
        errors = []
        if not self.url.startswith(("http://", "https://")):
            errors.append("The calendar URL must start with http:// or https://")
        errors += [f"Unknown impact {i!r}" for i in self.impacts if i not in IMPACTS]
        if not self.currencies:
            errors.append("Pick at least one currency")
        return errors

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> CalendarSettings:
        d = d or {}
        base = cls()
        return cls(
            enabled=bool(d.get("enabled", base.enabled)),
            url=(d.get("url") or base.url).strip(),
            currencies=[c.strip().upper() for c in d.get("currencies", base.currencies) if c and c.strip()],
            impacts=list(d.get("impacts", base.impacts)),
        )


def parse_events(payload: list[dict]) -> list[dict]:
    """ForexFactory rows -> events with a UTC time, oldest first. Rows without a usable time are skipped."""
    out = []
    for row in payload:
        try:
            when = datetime.fromisoformat(str(row["date"]))
        except (KeyError, ValueError):
            continue
        if when.tzinfo is None:
            continue  # all-day or tentative entries
        out.append({
            "title": str(row.get("title", "")).strip(),
            "currency": str(row.get("country", "")).strip().upper(),
            "impact": str(row.get("impact", "")).strip().capitalize(),
            "time": when.astimezone(timezone.utc).isoformat(),
            "forecast": (row.get("forecast") or None),
            "previous": (row.get("previous") or None),
        })
    return sorted(out, key=lambda e: e["time"])


def fetch_events(url: str, timeout: float = 15) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Wednesday calendar)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Calendar source returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Can't reach the calendar source: {getattr(exc, 'reason', exc)}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("The calendar source didn't return JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected calendar format (expected a list of events)")
    return parse_events(payload)


class Calendar:
    """Cached calendar; :meth:`snapshot` never blocks, it refreshes in the background when stale."""

    def __init__(self, store: Store | None, settings: CalendarSettings | None = None):
        self.store = store
        self.settings = settings or CalendarSettings()
        cache = store.get_setting(CALENDAR_CACHE_KEY) if store else None
        self.events: list[dict] = (cache or {}).get("events", [])
        self.fetched_at: str | None = (cache or {}).get("fetched_at")
        self.source: str | None = (cache or {}).get("url")
        self.error: str | None = None
        self._last_try: datetime | None = None
        self._lock = threading.Lock()
        self._busy = False

    def stale(self, now: datetime) -> bool:
        if self.source != self.settings.url or not self.fetched_at:
            return True
        return now - datetime.fromisoformat(self.fetched_at) >= REFRESH

    def refresh(self) -> None:
        """Fetch now (blocking)."""
        url = self.settings.url
        try:
            events = fetch_events(url)
        except RuntimeError as exc:
            self.error = str(exc)
            log.warning("calendar: %s", exc)
            return
        self.events, self.fetched_at, self.source, self.error = events, datetime.now(timezone.utc).isoformat(), url, None
        if self.store:
            self.store.set_setting(CALENDAR_CACHE_KEY, {"events": events, "fetched_at": self.fetched_at, "url": url})

    def _refresh_bg(self) -> None:
        try:
            self.refresh()
        finally:
            self._busy = False

    def maybe_refresh(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if not self.settings.enabled or not self.stale(now):
            return
        with self._lock:
            if self._busy or (self.error and self._last_try and now - self._last_try < RETRY):
                return
            self._busy = True
            self._last_try = now
        threading.Thread(target=self._refresh_bg, daemon=True, name="calendar").start()

    def upcoming(self, now: datetime | None = None, past: timedelta = timedelta(minutes=30)) -> list[dict]:
        """Matching events from ``past`` ago onwards (so a release stays visible for a while)."""
        now = now or datetime.now(timezone.utc)
        s = self.settings
        return [e for e in self.events
                if e["currency"] in s.currencies and e["impact"] in s.impacts
                and datetime.fromisoformat(e["time"]) >= now - past]

    def matching(self) -> list[dict]:
        """Every event in the cached week that matches the settings (for the chart)."""
        s = self.settings
        return [e for e in self.events if e["currency"] in s.currencies and e["impact"] in s.impacts]

    def snapshot(self) -> dict:
        self.maybe_refresh()
        source_matches = self.source == self.settings.url
        show = self.settings.enabled and source_matches
        return {
            "settings": self.settings.to_dict(),
            "events": self.upcoming() if show else [],
            "week": self.matching() if show else [],
            "fetched_at": self.fetched_at if source_matches else None,
            "error": self.error,
            "loading": self._busy,
        }
