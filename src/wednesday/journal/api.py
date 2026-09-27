"""HTTP routes of the journal (``/api/journals/...``)."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from .service import MULTI_FEATURE, JournalError, Journals

MAX_REPORT_BYTES = 50 * 1024 * 1024
SYNC_TIMEOUT = 90  # seconds to wait for the scan thread to read the terminal
TERMINAL_TIMEOUT = 15  # the same, for the logged-in account alone


def journal_router(get_journals: Callable[[], Journals | None], get_engine: Callable,
                   get_features: Callable[[], list[str]]) -> APIRouter:
    r = APIRouter(prefix="/api/journals")

    def svc() -> Journals:
        found = get_journals()
        if found is None:
            raise HTTPException(409, "The journal needs the server running with --serve and a database")
        return found

    def one(journal_id: str):
        try:
            return svc().get(journal_id)
        except KeyError:
            raise HTTPException(404, "No such journal") from None

    @r.get("")
    def list_journals() -> dict:
        found = get_journals()
        if found is None:
            return {"available": False, "journals": [], "multi": False}
        return {"available": True, "journals": found.list(), "multi": MULTI_FEATURE in get_features()}

    @r.post("")
    def create(body: dict = Body(...)) -> dict:
        try:
            return svc().create(str(body.get("name") or ""), get_features(), body.get("login"))
        except PermissionError as exc:
            raise HTTPException(402, str(exc)) from exc
        except JournalError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.get("/terminal")
    def terminal() -> dict:
        """The account the MT5 terminal is logged in to, so a new journal can be tied to it."""
        engine = get_engine()
        if not hasattr(engine.feed, "account_summary"):
            return {"connected": False, "detail": "The data source isn't MT5, so there's no terminal to sync from"}
        try:
            acc = engine.call(lambda feed: feed.account_summary(), timeout=TERMINAL_TIMEOUT)
        except TimeoutError:
            return {"connected": False, "detail": "The terminal didn't answer in time"}
        except RuntimeError as exc:
            return {"connected": False, "detail": str(exc)}
        return {"connected": True, **acc}

    @r.patch("/{journal_id}")
    def update(journal_id: str, body: dict = Body(...)) -> dict:
        one(journal_id)
        try:
            return svc().update(journal_id, body)
        except (JournalError, TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.delete("/{journal_id}")
    def delete(journal_id: str) -> dict:
        one(journal_id)
        return {"deleted": svc().delete(journal_id)}

    @r.get("/{journal_id}")
    def stats(journal_id: str) -> dict:
        one(journal_id)
        return svc().stats(journal_id)

    @r.post("/{journal_id}/sync")
    def sync(journal_id: str) -> dict:
        """Read the full deal history from the MT5 terminal the scanner is connected to."""
        one(journal_id)
        engine = get_engine()
        if not hasattr(engine.feed, "account_history"):
            raise HTTPException(409, "Syncing reads the MT5 terminal, and the data source isn't MT5. "
                                     "Switch it in Settings, or import the terminal's history report")
        try:
            history = engine.call(lambda feed: feed.account_history(), timeout=SYNC_TIMEOUT)
        except TimeoutError as exc:
            raise HTTPException(504, "The terminal didn't answer in time; try again") from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        try:
            return svc().sync(journal_id, history)
        except JournalError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.post("/{journal_id}/import")
    async def import_report(journal_id: str, request: Request) -> dict:
        """Upload the terminal's history report (raw body, HTML)."""
        one(journal_id)
        data = await request.body()
        if len(data) > MAX_REPORT_BYTES:
            raise HTTPException(413, "The report is larger than 50 MB")
        try:
            return svc().import_report(journal_id, data)
        except JournalError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.put("/{journal_id}/trades/{trade_id}/note")
    def note(journal_id: str, trade_id: str, body: dict = Body(...)) -> dict:
        one(journal_id)
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            raise HTTPException(422, "tags is a list")
        return svc().set_note(journal_id, trade_id, str(body.get("note") or ""), tags)

    @r.get("/{journal_id}/trades.csv")
    def trades_csv(journal_id: str) -> Response:
        j = one(journal_id)
        name = "".join(c if c.isalnum() else "-" for c in j["name"]).strip("-") or "journal"
        return Response(svc().trades_csv(journal_id), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="wednesday-{name}-trades.csv"'})

    return r
