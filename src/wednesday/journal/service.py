"""Journals as the server sees them: rows in the database, stats computed on demand and cached."""

from __future__ import annotations

import csv
import io
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from ..storage import Store, journal_cash_table, journal_notes_table, journal_trades_table
from . import sample as sample_data
from .imports import ReportError, parse_report
from .stats import analyse

MULTI_FEATURE = "journal.multi"
MARGIN = 15 * 3600  # load bars this far past a trade's ends: the deal and price clocks can differ by hours
MAX_IMAGES = 12  # chart snapshots per trade
CACHE_TTL = 120  # seconds; the stats also refresh at once when the journal's data changes
GOLD = {"XAUUSD", "XAU", "GOLD"}
SAMPLE_KEY = "journal.sample"  # setting: {"deleted": true} once the user deleted the sample portfolio
SAMPLE_ID = "sample"
SAMPLE_NAME = "Sample portfolio"


class JournalError(ValueError):
    """Something the user can fix, with a message for them."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(symbol: str) -> str:
    """Broker symbol without its suffix: XAUUSD.m, XAUUSDm and XAUUSD# are all XAUUSD; GOLD is XAUUSD."""
    base = re.split(r"[._#\-+!]", symbol.strip())[0] or symbol
    base = re.sub(r"(?<=[A-Z]{6})[a-z]+$", "", base).upper()
    return "XAUUSD" if base in GOLD else base


class Journals:
    def __init__(self, store: Store, sample: bool = False):
        """``sample``: add the sample portfolio the first time the journals are listed, unless it was deleted."""
        self.store = store
        self.sample = sample
        self._cache: dict[str, tuple[float, int, dict]] = {}
        self._version: dict[str, int] = {}
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._sample_checked = False

    # ---- journals ----
    def list(self, user: str | None = None) -> list[dict]:
        """The journals of one account (``user``), or all of them without a login."""
        if self.sample:
            self.ensure_sample()
        return [self._public(j) for j in self.journals_of(user)]

    def journals_of(self, user: str | None) -> list[dict]:
        found = self.store.journals()
        return found if user is None else [j for j in found if j.get("user_id") == user]

    def owned(self, journal_id: str, user: str | None) -> bool:
        """Whether journal ``journal_id`` is ``user``'s (any journal without a login)."""
        return any(j["id"] == journal_id for j in self.journals_of(user))

    # ---- the sample portfolio ----
    def ensure_sample(self) -> dict | None:
        """Make the sample portfolio (trades, cash and its own M1 bars) unless it's there or was deleted."""
        with self._sample_lock:
            if self._sample_checked:
                return None
            self._sample_checked = True
            if (self.store.get_setting(SAMPLE_KEY) or {}).get("deleted"):
                return None
            have = next((j for j in self.store.journals() if j.get("sample")), None)
            if have and self.store.bar_bounds(sample_data.SOURCE, sample_data.SYMBOL)[0]:
                return None
            data = sample_data.generate()
            self.store.delete_bars(sample_data.SOURCE)
            self.store.save_bars(sample_data.SOURCE, sample_data.SYMBOL, data["bars"])
            if have:
                return None
            row = {"id": SAMPLE_ID, "name": SAMPLE_NAME, "login": None, "server": None, "company": None,
                   "currency": "USD", "time_offset": None, "created_at": _now(),
                   "synced_at": None, "sample": True, "settings": None}
            self.store.journal_put(row)
            self.store.journal_fill(SAMPLE_ID, data["trades"], data["cash"], replace=True)
            self._changed(SAMPLE_ID)
            return self._public(row)

    def restore_sample(self) -> dict:
        """Bring the sample portfolio back after it was deleted."""
        self.store.delete_setting(SAMPLE_KEY)
        with self._sample_lock:
            self._sample_checked = False
        self.ensure_sample()
        return self._public(self.get(SAMPLE_ID))

    def get(self, journal_id: str) -> dict:
        for j in self.store.journals():
            if j["id"] == journal_id:
                return j
        raise KeyError(journal_id)

    @staticmethod
    def options(j: dict) -> dict:
        """The journal's own settings with their defaults."""
        saved = j.get("settings") or {}
        return {"show_weekends": bool(saved.get("show_weekends", False))}

    def _public(self, j: dict) -> dict:
        off = j.get("time_offset")
        out = {k: v for k, v in j.items() if k != "user_id"}  # whose it is stays on the server
        return {**out, **self.options(j), "settings": None,
                "time_offset": None if off is None else off / 3600, "sample": bool(j.get("sample"))}

    def create(self, name: str, features: list[str], login: str | None = None, user: str | None = None) -> dict:
        """A new journal of ``user`` (none without a login), tied to its MT5 account number from the
        start when one is given, so a report of another account is refused."""
        name = (name or "").strip()[:120]
        if not name:
            raise JournalError("Give the journal a name")
        login = str(login or "").strip() or None
        if login is not None and not login.isdigit():
            raise JournalError("The account number is the digits MT5 shows for the login, e.g. 51234567")
        existing = self.journals_of(user)
        if any(not j.get("sample") for j in existing) and MULTI_FEATURE not in features:  # the sample is free
            raise PermissionError("One journal is free. More than one needs a plan with multiple journals")
        taken = next((j for j in existing if login and str(j.get("login") or "") == login), None)
        if taken:
            raise JournalError(f"{taken['name']} already follows account {login}. Open it instead, or use another account")
        row = {"id": uuid.uuid4().hex[:12], "name": name, "login": login, "server": None, "company": None,
               "currency": None, "time_offset": None, "created_at": _now(),
               "synced_at": None, "settings": {}, "user_id": user}
        self.store.journal_put(row)
        return self._public(row)

    def update(self, journal_id: str, body: dict) -> dict:
        j = self.get(journal_id)
        if "name" in body:
            name = str(body["name"] or "").strip()[:120]
            if not name:
                raise JournalError("Give the journal a name")
            j["name"] = name
        if "time_offset" in body:
            off = body["time_offset"]
            if off is None or off == "":
                j["time_offset"] = None
            else:
                hours = float(off)
                if not -14 <= hours <= 14:
                    raise JournalError("The clock offset is between -14 and 14 hours")
                j["time_offset"] = int(round(hours * 3600))
        if "show_weekends" in body:
            if not isinstance(body["show_weekends"], bool):
                raise JournalError("show_weekends is true or false")
            j["settings"] = {**(j.get("settings") or {}), "show_weekends": body["show_weekends"]}
        self.store.journal_put(j)
        self._changed(journal_id)
        return self._public(j)

    def delete(self, journal_id: str) -> bool:
        """Delete a journal; the sample takes its bars along and stays deleted after a restart."""
        was_sample = bool(self.get(journal_id).get("sample"))
        self._changed(journal_id)
        deleted = self.store.journal_delete(journal_id)
        if was_sample:
            self.store.delete_bars(sample_data.SOURCE)
            self.store.set_setting(SAMPLE_KEY, {"deleted": True})
        return deleted

    @staticmethod
    def check_writable(j: dict) -> None:
        if j.get("sample"):
            raise JournalError("The sample portfolio is made-up data, so a report can't be imported into it. "
                               "Make a journal of your own for your account")

    # ---- imports ----
    def _check_account(self, j: dict, login: str | None) -> None:
        if j.get("login") and login and str(login) != str(j["login"]):
            raise JournalError(f"This journal is account {j['login']}, but that history is account {login}. "
                               "Make a journal per account")

    def import_report(self, journal_id: str, data: bytes) -> dict:
        j = self.get(journal_id)
        self.check_writable(j)
        try:
            parsed = parse_report(data)
        except ReportError as exc:
            raise JournalError(str(exc)) from exc
        acc = parsed["account"]
        self._check_account(j, acc.get("login"))
        self.store.journal_fill(journal_id, parsed["trades"], parsed["cash"], replace=True)
        j.update(login=acc.get("login") or j.get("login"), server=acc.get("server") or j.get("server"),
                 company=acc.get("company") or j.get("company"), currency=acc.get("currency") or j.get("currency"),
                 synced_at=_now())
        self.store.journal_put(j)
        self._changed(journal_id)
        return {"trades": len(parsed["trades"]), "cash": len(parsed["cash"]), "journal": self._public(j)}

    def set_note(self, journal_id: str, trade_id: str, note: str, tags: list[str]) -> dict:
        self.get(journal_id)
        note = (note or "").strip()[:5000]
        tags = list(dict.fromkeys(str(t).strip().lower()[:40] for t in (tags or []) if str(t).strip()))[:20]
        self.store.journal_note(journal_id, trade_id, note, tags, _now())
        self._changed(journal_id)
        return {"trade_id": trade_id, "note": note, "tags": tags}

    # ---- entries: notes, session reviews and journalled setups (#14) ----
    ENTRY_KINDS = ("note", "review", "setup")

    @staticmethod
    def _clean_entry(text, tags, mood) -> tuple[str, list[str], int | None]:
        text = str(text or "").strip()[:10_000]
        tags = list(dict.fromkeys(str(t).strip().lower()[:40] for t in (tags or []) if str(t).strip()))[:20]
        if mood not in (None, ""):
            mood = int(mood)
            if not 1 <= mood <= 5:
                raise JournalError("Mood is 1 to 5")
        else:
            mood = None
        return text, tags, mood

    def entries(self, journal_id: str) -> list[dict]:
        self.get(journal_id)
        return self.store.journal_entries(journal_id)

    def add_entry(self, journal_id: str, kind: str, text: str, tags: list, mood=None, setup: dict | None = None,
                  market: dict | None = None) -> dict:
        """A note, a session review or a setup from the ladder, with the market frozen as it is now."""
        self.get(journal_id)
        if kind not in self.ENTRY_KINDS:
            raise JournalError(f"An entry is one of {', '.join(self.ENTRY_KINDS)}")
        text, tags, mood = self._clean_entry(text, tags, mood)
        if kind != "setup" and not text:
            raise JournalError("Write something first")
        if kind == "setup" and not isinstance(setup, dict):
            raise JournalError("Journal a setup from a row of the ladder")
        row = {"id": uuid.uuid4().hex[:12], "journal_id": journal_id, "kind": kind, "created_at": _now(), "updated_at": None,
               "text": text, "tags": tags, "mood": mood, "setup": setup, "market": market}
        self.store.journal_entry_put(row)
        return row

    def update_entry(self, journal_id: str, entry_id: str, body: dict) -> dict:
        """Change the text, tags or mood; the frozen market never changes."""
        found = next((e for e in self.entries(journal_id) if e["id"] == entry_id), None)
        if found is None:
            raise KeyError(entry_id)
        text, tags, mood = self._clean_entry(body.get("text", found["text"]), body.get("tags", found["tags"]),
                                             body.get("mood", found["mood"]))
        found.update(text=text, tags=tags, mood=mood, updated_at=_now())
        self.store.journal_entry_put(found)
        return found

    def delete_entry(self, journal_id: str, entry_id: str) -> bool:
        self.get(journal_id)
        return self.store.journal_entry_delete(journal_id, entry_id)

    def _note(self, journal_id: str, trade_id: str) -> dict:
        found = [n for n in self.store.journal_rows(journal_notes_table, journal_id) if n["trade_id"] == trade_id]
        return found[0] if found else {"note": "", "tags": [], "images": []}

    def attach_image(self, journal_id: str, trade_id: str, snapshot_id: str) -> dict:
        """Add a chart snapshot to a trade's note (made if the trade has none)."""
        self.get(journal_id)
        if not any(t["id"] == trade_id for t in self.store.journal_rows(journal_trades_table, journal_id)):
            raise KeyError(trade_id)
        n = self._note(journal_id, trade_id)
        images = [*n["images"], snapshot_id][-MAX_IMAGES:] if snapshot_id not in n["images"] else n["images"]
        self.store.journal_note(journal_id, trade_id, n["note"], n["tags"], _now(), images)
        self._changed(journal_id)
        return {"trade_id": trade_id, "images": images}

    def detach_image(self, journal_id: str, trade_id: str, snapshot_id: str) -> dict:
        self.get(journal_id)
        n = self._note(journal_id, trade_id)
        if snapshot_id not in n["images"]:
            raise KeyError(snapshot_id)
        images = [i for i in n["images"] if i != snapshot_id]
        self.store.journal_note(journal_id, trade_id, n["note"], n["tags"], _now(), images)
        self._changed(journal_id)
        return {"trade_id": trade_id, "images": images}

    def images(self, journal_id: str) -> list[str]:
        """Every snapshot the journal's notes hold (to delete the files with the journal)."""
        return [i for n in self.store.journal_rows(journal_notes_table, journal_id) for i in n["images"]]

    # ---- stats ----
    def _changed(self, journal_id: str) -> None:
        with self._lock:
            self._version[journal_id] = self._version.get(journal_id, 0) + 1

    def price_bars(self, trades: list[dict], sample: bool = False) -> tuple[dict, dict]:
        """(symbol -> M1 bars, symbol -> "source:symbol" they came from) for the trades' symbols.
        The sample portfolio reads its own bars only, and real journals never read them."""
        stored = [s for s in self.store.bar_stats() if (s["source"] == sample_data.SOURCE) == sample]
        found, used = {}, {}
        for sym in sorted({t["symbol"] for t in trades}):
            want = canonical(sym)
            matches = [s for s in stored if s["symbol"] == sym] or [s for s in stored if canonical(s["symbol"]) == want]
            if not matches:
                continue
            best = max(matches, key=lambda s: s["bars"])
            mine = [t for t in trades if t["symbol"] == sym]
            start = min(t["open_time"] for t in mine) - MARGIN
            end = max(t["close_time"] or int(time.time()) + 86400 for t in mine) + MARGIN
            df = self.store.load_bar_range(best["source"], best["symbol"], start, end)
            if len(df):
                found[sym], used[sym] = df, f"{best['source']}:{best['symbol']}"
        return found, used

    def stats(self, journal_id: str) -> dict:
        j = self.get(journal_id)
        version = self._version.get(journal_id, 0)
        hit = self._cache.get(journal_id)
        if hit and hit[1] == version and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[2]
        trades = self.store.journal_rows(journal_trades_table, journal_id)
        cash = self.store.journal_rows(journal_cash_table, journal_id)
        notes = {n["trade_id"]: n for n in self.store.journal_rows(journal_notes_table, journal_id)}
        bars, used = self.price_bars(trades, bool(j.get("sample")))
        result = analyse(trades, cash, bars, j.get("time_offset"))
        for t in result["trades"]:
            n = notes.get(t["id"])
            t["note"], t["tags"], t["images"] = (n["note"], n["tags"], n["images"]) if n else ("", [], [])
            t.pop("journal_id", None)
        result["verification"]["prices_from"] = used
        result["journal"] = self._public(j)
        result["cash"] = cash
        for c in cash:
            c.pop("journal_id", None)
        self._cache[journal_id] = (time.monotonic(), version, result)
        return result

    def trades_csv(self, journal_id: str) -> str:
        data = self.stats(journal_id)
        buf = io.StringIO()
        cols = ["id", "position", "symbol", "side", "volume", "open_time", "open_price", "close_time", "close_price",
                "profit", "commission", "swap", "net", "mae", "mfe", "verified", "price_ok", "tags", "note"]
        w = csv.writer(buf)
        w.writerow(cols)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else ""  # noqa: E731
        for t in data["trades"]:
            row = {**t, "open_time": iso(t["open_time"]), "close_time": iso(t["close_time"]), "tags": " ".join(t["tags"])}
            w.writerow([row.get(c, "") if row.get(c) is not None else "" for c in cols])
        return buf.getvalue()
