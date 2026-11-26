"""An account's dashboard preferences, and what a browser kept during the preview, into an account.

**Preferences** are the dashboard's own ``wed.`` and ``xau.`` keys (layout, chart type...) that a
browser would otherwise keep in its local storage. They are saved per account (``prefs:<user id>``;
``prefs`` without a login), so they follow the trader to another browser.

**The import** (``POST /api/me/import``): a guest's drawings, templates, workspaces and preferences
live in the browser. Once they log in or sign up, the dashboard sends them here; what the account
already has (a template or workspace of that name, a preference it set) is left alone.
"""

from __future__ import annotations

import json

from .drawing_templates import FAMILIES, MAX_TEMPLATES, DrawingTemplates, TemplateError
from .storage import user_key
from .workspaces import MAX_WORKSPACES, WorkspaceError, Workspaces

PREFS_KEY = "prefs"
MAX_PREFS_BYTES = 256 * 1024
_PREFIXES = ("wed.", "xau.")


class PrefsError(ValueError):
    """Something the dashboard sent wrong, with a message."""


def _squeeze(name) -> str:
    """A name as templates and workspaces store it."""
    return " ".join(str(name).split())[:60]


def get_prefs(store, user: str | None) -> dict:
    return store.get_setting(user_key(PREFS_KEY, user)) or {}


def put_prefs(store, user: str | None, body: dict) -> dict:
    """Merge ``{key: value}`` into the saved preferences; a null value removes the key."""
    prefs = get_prefs(store, user)
    for k, v in body.items():
        if not str(k).startswith(_PREFIXES) or len(k) > 100:
            raise PrefsError(f"Unknown preference {k!r}: they start with wed.")
        if v is None:
            prefs.pop(k, None)
        else:
            prefs[k] = v
    if len(json.dumps(prefs)) > MAX_PREFS_BYTES:
        raise PrefsError("The preferences are too large to save")
    store.set_setting(user_key(PREFS_KEY, user), prefs)
    return prefs


def import_settings(store, user: str | None, body: dict) -> tuple[int, int, int]:
    """Templates and workspaces whose name the account doesn't have yet, and the preferences it
    hasn't set: how many of each were added."""
    templates = workspaces = prefs = 0
    saved = DrawingTemplates(store, user)
    have = saved._all()
    sent = body.get("templates")
    for t in (sent if isinstance(sent, list) else [])[: MAX_TEMPLATES * len(set(FAMILIES.values()))]:
        if not isinstance(t, dict) or not isinstance(t.get("family"), str) or not isinstance(t.get("name"), str):
            continue
        if _squeeze(t["name"]) in (have.get(t["family"]) or {}):
            continue
        try:
            saved.save(t["family"], t["name"], t)
        except TemplateError:
            continue
        templates += 1
    spaces = Workspaces(store, user)
    have = spaces._all()
    sent = body.get("workspaces")
    for w in (sent if isinstance(sent, list) else [])[:MAX_WORKSPACES]:
        if not isinstance(w, dict) or not isinstance(w.get("name"), str) or _squeeze(w["name"]) in have:
            continue
        try:
            spaces.save(w["name"], w.get("layout") or {}, w.get("windows") or [])
        except WorkspaceError:
            continue
        workspaces += 1
    sent = body.get("prefs")
    if isinstance(sent, dict):
        have = get_prefs(store, user)
        new = {k: v for k, v in sent.items() if k not in have and v is not None and str(k).startswith(_PREFIXES)}
        prefs = len(new)
        if new:
            put_prefs(store, user, new)
    return templates, workspaces, prefs
