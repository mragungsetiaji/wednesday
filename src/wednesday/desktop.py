"""Desktop app: the dashboard in its own window, for the Windows installer.

Runs the same scan loop and API as ``wednesday --serve``, with uvicorn on a
background thread and the dashboard in a native window (pywebview, WebView2 on
Windows). Closing the window stops everything.

Settings, the database, models and logs live in a per-user folder, not next to
the program, so the app can be installed without admin rights and upgraded
without touching data: ``%LOCALAPPDATA%\\Wednesday`` on Windows (``~/.wednesday``
elsewhere, or ``WEDNESDAY_HOME``). Its ``.env`` is created from ``.env.example``
on first run; secrets (MT5 password, Telegram token, LLM keys) go there.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .cli import load_env_file, main

log = logging.getLogger("wednesday")

TITLE = "Wednesday"


def home_dir() -> Path:
    if custom := os.environ.get("WEDNESDAY_HOME"):
        return Path(custom)
    if os.name == "nt" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "Wednesday"
    return Path.home() / ".wednesday"


def bundle_dir() -> Path:
    """Where bundled files are: PyInstaller's unpack folder, or the repository root when run from source."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


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
        webview.create_window(TITLE, url, width=1440, height=900, min_size=(960, 600))
        # private_mode=False keeps the dashboard's saved layout (localStorage) between runs.
        webview.start(private_mode=False, storage_path=str(home_dir() / "webview"))
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

    # Relative paths in .env (data/xau.db, data/models, logs/) resolve inside the home folder.
    os.chdir(home)
    env = home / ".env"
    example = bundle_dir() / ".env.example"
    if not env.exists() and example.is_file():
        shutil.copyfile(example, env)
    load_env_file(env)
    os.environ.setdefault("XAU_LOG_FILE", "logs/screener.log")

    host = os.environ.get("XAU_HOST") or "127.0.0.1"
    port = _pick_port(host, int(os.environ.get("XAU_PORT") or 8000))
    try:
        main(["--serve", "--env-file", str(env), "--host", host, "--port", str(port)], serve=serve_in_window)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _alert(f"{exc.code}\n\nSettings are in {env}")
        raise
    except Exception as exc:
        log.exception("desktop app stopped")
        _alert(f"Wednesday stopped: {exc}\n\nThe log is in {home / 'logs'}")
        raise
    finally:
        lock.close()


if __name__ == "__main__":
    run()
