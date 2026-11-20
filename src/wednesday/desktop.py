"""The desktop app's server: the Tauri shell (``src-tauri``) starts it as its sidecar.

Runs the same scan loop and API as ``wednesday --serve``, without a window: the shell shows the
dashboard in its own windows and stops this when it closes.

The database, models and logs live in a per-user folder, not next to the
program, so the app can be installed without admin rights and upgraded without
touching data: ``%LOCALAPPDATA%\\Wednesday`` on Windows (``~/.wednesday``
elsewhere, or ``WEDNESDAY_HOME``). Everything is set in the dashboard; API keys
and bot tokens go to the OS credential store. A ``.env`` in that folder is still
read if one is there, but the app doesn't need or create one.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from .cli import load_env_file, main

log = logging.getLogger("wednesday")


def home_dir() -> Path:
    if custom := os.environ.get("WEDNESDAY_HOME"):
        return Path(custom)
    if os.name == "nt" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "Wednesday"
    return Path.home() / ".wednesday"


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


def _prepare_home() -> tuple[Path, object | None, Path]:
    """The per-user data folder as the working directory, its lock and its ``.env``.

    The lock is None when another copy already runs there.
    """
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    # A windowed exe has no console: print() and logging would fail on a missing stdout.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    lock = _single_instance(home)
    # Relative paths (data/xau.db, data/models, logs/) resolve inside the home folder.
    os.chdir(home)
    env = home / ".env"
    load_env_file(env)
    os.environ.setdefault("XAU_LOG_FILE", "logs/screener.log")
    return home, lock, env


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _exit_with_parent(pid: int) -> None:
    """Stop this process once ``pid`` (the desktop shell) is gone, so a crashed shell leaves no server behind."""
    def watch() -> None:
        while _process_alive(pid):
            time.sleep(2)
        log.info("the desktop shell (pid %s) is gone: stopping", pid)
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def run_sidecar(argv: list[str] | None = None) -> None:
    """The API and dashboard server for the Tauri desktop shell (``src-tauri``), without a window.

    The shell picks the port, starts this, waits for the port to answer and shows the dashboard in
    its own windows.
    """
    import argparse

    p = argparse.ArgumentParser(prog="wednesday-server", description=run_sidecar.__doc__)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--parent-pid", type=int, default=None, help="stop when this process (the shell) exits")
    args = p.parse_args(argv)
    _home, lock, env = _prepare_home()
    if lock is None:
        raise SystemExit("Wednesday is already running.")
    if args.parent_pid:
        _exit_with_parent(args.parent_pid)
    try:
        main(["--serve", "--env-file", str(env), "--host", "127.0.0.1", "--port", str(args.port)])
    finally:
        lock.close()
