"""Desktop app: the dashboard in its own window, for the Windows installer.

Runs the same scan loop and API as ``wednesday --serve``, with uvicorn on a
background thread and the dashboard in a native window (pywebview, WebView2 on
Windows). Closing the window stops everything.

The database, models and logs live in a per-user folder, not next to the
program, so the app can be installed without admin rights and upgraded without
touching data: ``%LOCALAPPDATA%\\Wednesday`` on Windows (``~/.wednesday``
elsewhere, or ``WEDNESDAY_HOME``). Everything is set in the dashboard; the MT5
password goes to Windows Credential Manager. A ``.env`` in that folder is still
read if one is there, but the app doesn't need or create one.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .cli import load_env_file, main
from .workspaces import MIN_SIZE, ROUTE, WorkspaceError, Workspaces, clean_windows, fit_on_screen

log = logging.getLogger("wednesday")

TITLE = "Wednesday"


def home_dir() -> Path:
    if custom := os.environ.get("WEDNESDAY_HOME"):
        return Path(custom)
    if os.name == "nt" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "Wednesday"
    return Path.home() / ".wednesday"


def _alert(message: str) -> None:
    """Show an error with no console attached (a message box on Windows)."""
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
    else:
        print(message, file=sys.stderr)


def _single_instance(home: Path):
    """Hold a lock file for the app's lifetime; None when another copy already holds it.

    Two copies would scan twice into the same database and send every Telegram alert twice.
    """
    try:
        fh = open(home / "wednesday.lock", "w")
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        if os.name != "nt":
            # As uvicorn binds: a port left in TIME_WAIT by the last run is still usable.
            # (On Windows SO_REUSEADDR would take a port that is in use, so not there.)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, preferred: int) -> int:
    # Keep the same port between runs when possible: the dashboard's layout and
    # preferences are kept in the browser storage of that origin.
    if _port_free(host, preferred):
        return preferred
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class WindowTracker:
    """The app's windows with their routes, so they can be remembered and arranged (#27)."""

    def __init__(self, url: str):
        self.url = url
        self.routes: dict = {}  # window -> the route it opened with
        self.maximized: dict = {}
        self.main = None

    def track(self, w, route: str):
        self.routes[w] = route
        self.maximized[w] = False
        events = getattr(w, "events", None)
        for name, value in (("maximized", True), ("restored", False)):
            ev = getattr(events, name, None)
            if ev is not None:
                ev += lambda w=w, value=value: self.maximized.__setitem__(w, value)
        closed = getattr(events, "closed", None)
        if closed is not None and w is not self.main:
            closed += lambda w=w: (self.routes.pop(w, None), self.maximized.pop(w, None))
        return w

    def route_of(self, w) -> str:
        """The window's route now (a pop-out's timeframe can change), falling back to the one it opened with."""
        try:
            url = w.get_current_url() or ""
        except Exception:  # noqa: BLE001 - closing, or an old pywebview
            url = ""
        route = "#" + url.split("#", 1)[1] if "#" in url else self.routes.get(w, "#")
        return route if ROUTE.match(route) else self.routes.get(w, "#")

    def snapshot(self) -> list[dict]:
        """Every open window: route, position, size, maximised. The dashboard first."""
        out = []
        for w in sorted(self.routes, key=lambda w: w is not self.main):
            out.append({"route": "#" if w is self.main else self.route_of(w), "x": int(getattr(w, "x", 0) or 0),
                        "y": int(getattr(w, "y", 0) or 0), "width": int(getattr(w, "width", 1440) or 1440),
                        "height": int(getattr(w, "height", 900) or 900), "maximized": bool(self.maximized.get(w))})
        return out

    @staticmethod
    def screens() -> list[dict]:
        import webview

        return [{"x": int(getattr(s, "x", 0)), "y": int(getattr(s, "y", 0)), "width": int(s.width), "height": int(s.height)}
                for s in getattr(webview, "screens", None) or []]

    def open(self, route: str, api, geometry: dict | None = None):
        """A pop-out window for ``route``, where ``geometry`` says (kept on a screen that exists)."""
        import webview

        g = fit_on_screen(geometry, self.screens()) if geometry else {"width": 1100, "height": 700}
        tf = route.split("/")[1] if route.count("/") else ""
        kw = {k: g[k] for k in ("x", "y", "width", "height") if k in g}
        w = webview.create_window(f"{TITLE} {tf}".strip(), f"{self.url}/{route}", min_size=MIN_SIZE, js_api=api, **kw)
        self.track(w, route)
        if g.get("maximized") and hasattr(w, "maximize"):
            w.maximize()
        return w

    def place(self, w, geometry: dict) -> None:
        """Move and resize an open window (the dashboard's) to ``geometry``."""
        g = fit_on_screen(geometry, self.screens())
        if g.get("maximized") and hasattr(w, "maximize"):
            w.maximize()
            return
        if hasattr(w, "restore") and self.maximized.get(w):
            w.restore()
        if hasattr(w, "move"):
            w.move(g["x"], g["y"])
        if hasattr(w, "resize"):
            w.resize(g["width"], g["height"])

    def arrange(self, windows: list[dict], api) -> None:
        """Make the open windows match ``windows``: the dashboard moves, pop-outs close and reopen."""
        for w in list(self.routes):
            if w is not self.main:
                w.destroy()
                self.routes.pop(w, None)
        for g in windows:
            if g["route"] in ("#", ""):
                if self.main is not None:
                    self.place(self.main, g)
            else:
                self.open(g["route"], api, g)


class DesktopApi:
    """Called from the dashboard as ``window.pywebview.api.<method>()``; only in the desktop app."""

    def __init__(self, url: str = "", tracker: WindowTracker | None = None):
        self.url = url  # the local dashboard, for the pop-out chart windows
        self.tracker = tracker or WindowTracker(url)

    def open_chart(self, tf: str, group: str | None = None) -> bool:
        """Open one chart in its own window (another monitor, say). It loads the same local dashboard,
        so the scan, ticks and drawings are shared; it closes with the main window."""
        tf = "".join(c for c in str(tf) if c.isalnum())[:8] or "1H"
        group = group if group in ("A", "B", "C") else None
        route = f"#popout/{tf}" + (f"/{group}" if group else "")
        self.tracker.open(route, self)
        return True

    def window_layout(self) -> list[dict]:
        """The open windows, for saving a workspace."""
        return self.tracker.snapshot()

    def arrange(self, windows: list) -> bool:
        """Open a workspace's windows: move the dashboard, replace the pop-outs."""
        try:
            self.tracker.arrange(clean_windows(windows), self)
        except WorkspaceError:
            return False
        return True

    def pick_terminal(self) -> str | None:
        """Pick a terminal64.exe with the Windows file dialog."""
        import webview

        start = os.environ.get("ProgramFiles") or str(Path.home())
        dialog = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
        picked = webview.windows[0].create_file_dialog(dialog, directory=start, file_types=("Programs (*.exe)",))
        return picked[0] if picked else None


    def save_png(self, data: str, name: str) -> str | None:
        """Save a chart snapshot (base64 PNG) where the trader picks; WebView2 has no downloads."""
        import base64

        import webview

        safe = "".join(c for c in os.path.basename(name or "") if c.isalnum() or c in "-_.") or "wednesday.png"
        if not safe.lower().endswith(".png"):
            safe += ".png"
        dialog = webview.FileDialog.SAVE if hasattr(webview, "FileDialog") else webview.SAVE_DIALOG
        picked = webview.windows[0].create_file_dialog(dialog, directory=str(Path.home() / "Pictures"), save_filename=safe,
                                                       file_types=("PNG images (*.png)",))
        if not picked:
            return None
        path = Path(picked if isinstance(picked, str) else picked[0])
        path.write_bytes(base64.b64decode(data))
        return str(path)


def serve_in_window(app, host: str, port: int) -> None:
    """Start uvicorn on a thread, show the dashboard in a window, stop the server when it closes."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise SystemExit(f"The dashboard could not start on port {port}. See the log in {home_dir() / 'logs'}.")
        time.sleep(0.05)

    url = f"http://127.0.0.1:{port}"
    try:
        try:
            import webview
        except ImportError:
            log.warning("pywebview is not installed (uv sync --extra desktop); opening %s in the browser", url)
            webbrowser.open(url)
            thread.join()
            return
        store = getattr(app.state, "store", None)
        memory = Workspaces(store) if store is not None else None
        last = memory.last_windows() if memory else []
        tracker = WindowTracker(url)
        api = DesktopApi(url, tracker)
        first = next((w for w in last if w["route"] == "#"), None)
        geometry = {k: first[k] for k in ("x", "y", "width", "height")} if first else {"width": 1440, "height": 900}
        main = webview.create_window(TITLE, url, min_size=(960, 600), js_api=api, **geometry)
        tracker.main = main
        tracker.track(main, "#")

        def restore() -> None:  # once the GUI runs: keep the dashboard on screen, reopen last session's charts
            if first:
                tracker.place(main, first)
            for w in last:
                if w["route"] != "#":
                    tracker.open(w["route"], api, w)

        def remember() -> None:  # the dashboard is closing: keep where every window was
            if memory:
                try:
                    memory.remember_windows(tracker.snapshot())
                except Exception:  # noqa: BLE001 - never block closing over it
                    log.exception("couldn't remember the windows")

        def close_popouts() -> None:  # the server stops with the main window, so its charts go too
            for w in list(webview.windows):
                if w is not main:
                    w.destroy()

        if getattr(main.events, "closing", None) is not None:
            main.events.closing += remember
        main.events.closed += close_popouts
        # private_mode=False keeps the dashboard's saved layout (localStorage) between runs.
        webview.start(restore, private_mode=False, storage_path=str(home_dir() / "webview"))
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def run() -> None:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    # A windowed exe has no console: print() and logging would fail on a missing stdout.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    lock = _single_instance(home)
    if lock is None:
        _alert("Wednesday is already running.")
        return

    # Relative paths (data/xau.db, data/models, logs/) resolve inside the home folder.
    os.chdir(home)
    env = home / ".env"
    load_env_file(env)
    os.environ.setdefault("XAU_LOG_FILE", "logs/screener.log")

    host = os.environ.get("XAU_HOST") or "127.0.0.1"
    port = _pick_port(host, int(os.environ.get("XAU_PORT") or 8000))
    try:
        main(["--serve", "--env-file", str(env), "--host", host, "--port", str(port)], serve=serve_in_window)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _alert(f"{exc.code}\n\nThe log is in {home / 'logs'}")
        raise
    except Exception as exc:
        log.exception("desktop app stopped")
        _alert(f"Wednesday stopped: {exc}\n\nThe log is in {home / 'logs'}")
        raise
    finally:
        lock.close()


if __name__ == "__main__":
    run()
