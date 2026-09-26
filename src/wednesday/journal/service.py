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
from .imports import ReportError, from_deals, parse_report
from .stats import analyse

MULTI_FEATURE = "journal.multi"
MARGIN = 15 * 3600  # load bars this far past a trade's ends: the deal and price clocks can differ by hours
CACHE_TTL = 120  # seconds; the stats also refresh at once when the journal's data changes
GOLD = {"XAUUSD", "XAU", "GOLD"}


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
    def __init__(self, store: Store):
        self.store = store
        self._cache: dict[str, tuple[float, int, dict]] = {}
        self._version: dict[str, int] = {}
        self._lock = threading.Lock()

    # ---- journals ----
    def list(self) -> list[dict]:
        return [self._public(j) for j in self.store.journals()]

    def get(self, journal_id: str) -> dict:
        for j in self.store.journals():
            if j["id"] == journal_id:
                return j
        raise KeyError(journal_id)

    @staticmethod
    def _public(j: dict) -> dict:
        off = j.get("time_offset")
        return {**j, "time_offset": None if off is None else off / 3600}

    def create(self, name: str, features: list[str]) -> dict:
        name = (name or "").strip()[:120]
        if not name:
            raise JournalError("Give the journal a name")
        if self.store.journals() and MULTI_FEATURE not in features:
            raise PermissionError("One journal is free. More than one needs a plan with multiple journals")
        row = {"id": uuid.uuid4().hex[:12], "name": name, "login": None, "server": None, "company": None,
               "currency": None, "source": None, "account": None, "time_offset": None, "created_at": _now(),
               "synced_at": None}
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
        self.store.journal_put(j)
        self._changed(journal_id)
        return self._public(j)

    def delete(self, journal_id: str) -> bool:
        self._changed(journal_id)
        return self.store.journal_delete(journal_id)

    # ---- imports ----
    def _check_account(self, j: dict, login: str | None) -> None:
        if j.get("login") and login and str(login) != str(j["login"]):
            raise JournalError(f"This journal is account {j['login']}, but that history is account {login}. "
                               "Make a journal per account")

    def sync(self, journal_id: str, history: dict) -> dict:
        """Replace the journal's trades with the terminal's full history (see ``MT5Feed.account_history``)."""
        j = self.get(journal_id)
        acc = history.get("account") or {}
        self._check_account(j, acc.get("login"))
        trades, cash = from_deals(history.get("deals") or [], history.get("positions") or [])
        self.store.journal_fill(journal_id, trades, cash, replace=True)
        j.update(login=str(acc.get("login") or j.get("login") or "") or None, server=acc.get("server") or j.get("server"),
                 company=acc.get("company") or j.get("company"), currency=acc.get("currency") or j.get("currency"),
                 source="mt5", synced_at=_now(),
                 account={"balance": acc.get("balance"), "equity": acc.get("equity"), "at": _now()})
        self.store.journal_put(j)
        self._changed(journal_id)
        return {"trades": len(trades), "cash": len(cash), "journal": self._public(j)}

    def import_report(self, journal_id: str, data: bytes) -> dict:
        j = self.get(journal_id)
        try:
            parsed = parse_report(data)
        except ReportError as exc:
            raise JournalError(str(exc)) from exc
        acc = parsed["account"]
        self._check_account(j, acc.get("login"))
        self.store.journal_fill(journal_id, parsed["trades"], parsed["cash"], replace=True)
        j.update(login=acc.get("login") or j.get("login"), server=acc.get("server") or j.get("server"),
                 company=acc.get("company") or j.get("company"), currency=acc.get("currency") or j.get("currency"),
                 source="report", synced_at=_now(), account=None)
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

    # ---- stats ----
    def _changed(self, journal_id: str) -> None:
        with self._lock:
            self._version[journal_id] = self._version.get(journal_id, 0) + 1

    def price_bars(self, trades: list[dict]) -> tuple[dict, dict]:
        """(symbol -> M1 bars, symbol -> "source:symbol" they came from) for the trades' symbols."""
        stored = self.store.bar_stats()
        found, used = {}, {}
        for sym in sorted({t["symbol"] for t in trades}):
            want = canonical(sym)
            matches = [s for s in stored if s["symbol"] == sym] or [s for s in stored if canonical(s["symbol"]) == want]
            if not matches:
                continue
            best = max(matches, key=lambda s: (s["source"] == "mt5", s["bars"]))
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
        bars, used = self.price_bars(trades)
        result = analyse(trades, cash, bars, j.get("time_offset"))
        for t in result["trades"]:
            n = notes.get(t["id"])
            t["note"], t["tags"] = (n["note"], n["tags"]) if n else ("", [])
            t.pop("journal_id", None)
        result["verification"]["prices_from"] = used
        acc = j.get("account") or {}
        if acc.get("balance") is not None:
            result["verification"]["balance_reported"] = acc["balance"]
            result["verification"]["balance_matches"] = abs(acc["balance"] - result["summary"]["balance"]) < 0.01
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
