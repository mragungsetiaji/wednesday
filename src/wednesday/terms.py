"""The disclaimer and risk agreement the trader accepts before using the dashboard.

The text is DISCLAIMER.md at the repository root (bundled next to the code in
the desktop app). Acceptance is stored with the version of the terms, so a
change to them asks again. With the login on, each account accepts them
(``terms:<user id>``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .storage import Store, user_key

TERMS_KEY = "terms"
# Bump when DISCLAIMER.md changes in substance: everyone is asked to accept again.
TERMS_VERSION = "2026-09-26"


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def disclaimer_text() -> str:
    try:
        return (_root() / "DISCLAIMER.md").read_text(encoding="utf-8")
    except OSError:
        return ("# Disclaimer and risk agreement\n\nWednesday is not financial advice. Trading carries a high risk "
                "of loss, and every trading decision and its result is yours alone. The software is provided "
                "as is, without warranty, and the author is not liable for any loss.")


class Terms:
    def __init__(self, store: Store | None):
        self.store = store
        self._accepted: dict | None = None  # without a database: for this run only

    def accepted(self, user: str | None = None) -> dict | None:
        saved = self.store.get_setting(user_key(TERMS_KEY, user)) if self.store else self._accepted
        return saved if saved and saved.get("version") == TERMS_VERSION else None

    def accept(self, user: str | None = None) -> dict:
        record = {"version": TERMS_VERSION, "accepted_at": datetime.now(timezone.utc).isoformat()}
        if self.store:
            self.store.set_setting(user_key(TERMS_KEY, user), record)
        else:
            self._accepted = record
        return record

    def status(self, user: str | None = None) -> dict:
        accepted = self.accepted(user)
        return {"version": TERMS_VERSION, "accepted": accepted is not None,
                "accepted_at": accepted["accepted_at"] if accepted else None, "text": disclaimer_text()}
