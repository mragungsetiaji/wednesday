"""Window and layout memory (#27): where the desktop app's windows were, and named workspaces.

A **window** is a route (``#`` for the dashboard, ``#popout/5M/A`` for a popped-out chart) with its
position, size, whether it was maximised and the screen it was on. The desktop app keeps the last
session's windows (``LAST_KEY``) and restores them on start. A **workspace** is a named set of windows
plus the chart layout (the dashboard's layout preferences), saved from the chart and reopened from it.
Both are settings in the database, not WebView storage, so clearing ``webview\\`` doesn't lose them.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

LAST_KEY = "desktop.windows"  # the desktop app's windows when it last closed
WORKSPACES_KEY = "workspaces"  # name -> {"layout", "windows", "saved_at"}
MAX_WORKSPACES = 20
MAX_WINDOWS = 12
MAX_LAYOUT_BYTES = 64 * 1024
ROUTE = re.compile(r"^#?(popout/[A-Za-z0-9]{1,8}(/[ABC])?)?$")
MIN_SIZE = (480, 320)


class WorkspaceError(ValueError):
    """Something the trader can fix, with a message for them."""


def clean_window(w: dict) -> dict:
    """A window as stored: a known route and whole-pixel geometry."""
    route = str(w.get("route") or "#")
    if not ROUTE.match(route):
        raise WorkspaceError(f"Unknown window {route!r}")
    out = {"route": route if route.startswith("#") else f"#{route}", "maximized": bool(w.get("maximized"))}
    for k in ("x", "y", "width", "height"):
        v = w.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise WorkspaceError(f"A window needs a numeric {k}")
        out[k] = int(v)
    out["width"], out["height"] = max(out["width"], MIN_SIZE[0]), max(out["height"], MIN_SIZE[1])
    return out


def clean_windows(windows) -> list[dict]:
    if not isinstance(windows, list):
        raise WorkspaceError("windows is a list")
    if len(windows) > MAX_WINDOWS:
        raise WorkspaceError(f"At most {MAX_WINDOWS} windows in a workspace")
    return [clean_window(w) for w in windows if isinstance(w, dict)]


def clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise WorkspaceError("Give the workspace a name, e.g. London prep")
    return name


def clean_layout(layout) -> dict:
    """The dashboard's layout preferences (key -> JSON value), only ``wed.`` and ``xau.`` keys."""
    if not isinstance(layout, dict):
        raise WorkspaceError("layout is an object")
    out = {str(k): v for k, v in layout.items() if str(k).startswith(("wed.", "xau."))}
    if len(json.dumps(out)) > MAX_LAYOUT_BYTES:
        raise WorkspaceError("The layout is too large to save")
    return out


def fit_on_screen(w: dict, screens: list[dict]) -> dict:
    """Keep a window reachable: if its middle isn't on any screen (a monitor was unplugged), move it
    onto the first (primary) screen and shrink it to fit."""
    if not screens:
        return w
    cx, cy = w["x"] + w["width"] // 2, w["y"] + w["height"] // 2
    for s in screens:
        if s["x"] <= cx < s["x"] + s["width"] and s["y"] <= cy < s["y"] + s["height"]:
            return w
    p = screens[0]
    width, height = min(w["width"], p["width"]), min(w["height"], p["height"])
    return {**w, "x": p["x"] + (p["width"] - width) // 2, "y": p["y"] + (p["height"] - height) // 2,
            "width": width, "height": height}


class Workspaces:
    def __init__(self, store):
        self.store = store

    def _all(self) -> dict:
        return self.store.get_setting(WORKSPACES_KEY) or {}

    def list(self) -> list[dict]:
        return [{"name": n, "saved_at": w.get("saved_at"), "windows": len(w.get("windows") or [])}
                for n, w in sorted(self._all().items(), key=lambda kv: kv[0].lower())]

    def get(self, name: str) -> dict:
        found = self._all().get(name)
        if found is None:
            raise KeyError(name)
        return {"name": name, **found}

    def save(self, name: str, layout, windows) -> dict:
        name = clean_name(name)
        items = self._all()
        if name not in items and len(items) >= MAX_WORKSPACES:
            raise WorkspaceError(f"At most {MAX_WORKSPACES} workspaces: remove one first")
        items[name] = {"layout": clean_layout(layout), "windows": clean_windows(windows or []),
                       "saved_at": datetime.now(timezone.utc).isoformat()}
        self.store.set_setting(WORKSPACES_KEY, items)
        return {"name": name, **items[name]}

    def delete(self, name: str) -> bool:
        items = self._all()
        if items.pop(name, None) is None:
            return False
        self.store.set_setting(WORKSPACES_KEY, items)
        return True

    # ---- the desktop app's last session ----
    def last_windows(self) -> list[dict]:
        try:
            return clean_windows(self.store.get_setting(LAST_KEY) or [])
        except WorkspaceError:
            return []

    def remember_windows(self, windows: list[dict]) -> None:
        self.store.set_setting(LAST_KEY, clean_windows(windows))
