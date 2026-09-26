"""Find MetaTrader 5 terminals on this PC, and tell whether one is running.

A trader often has several: MetaQuotes' own and one per broker, each in its own
folder with its own ``terminal64.exe``. Settings lists them so the right one
can be picked, and the feed checks whether the chosen one is still open before
reconnecting, so it never starts a terminal the trader closed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

EXE = "terminal64.exe"


def normalize_path(path: str | None) -> str | None:
    """A pasted path as a terminal64.exe path: quotes from "Copy as path" removed, a folder completed."""
    if path is None:
        return None
    path = path.strip().strip('"').strip()
    if not path:
        return None
    if not path.lower().endswith(".exe"):
        path = os.path.join(path, EXE)
    return path


def _same(a: str) -> str:
    return os.path.normcase(os.path.abspath(a))


def _origin(folder: Path) -> Path | None:
    """The install folder written in a terminal data folder's origin.txt (UTF-16 on most installs)."""
    try:
        raw = (folder / "origin.txt").read_bytes()
    except OSError:
        return None
    for enc in ("utf-16", "utf-8"):
        try:
            text = raw.decode(enc).strip().strip("﻿")
        except UnicodeDecodeError:
            continue
        if text:
            return Path(text)
    return None


def find_terminals() -> list[dict]:
    """Installed MT5 terminals: ``{"path", "name", "running"}``, running ones first. Empty off Windows."""
    if sys.platform != "win32":
        return []
    candidates: list[Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if root := os.environ.get(var):
            candidates += Path(root).glob(f"*/{EXE}")
    if local := os.environ.get("LOCALAPPDATA"):
        candidates += Path(local, "Programs").glob(f"*/{EXE}")
    if appdata := os.environ.get("APPDATA"):
        # Every terminal that has run keeps a data folder here naming its install folder.
        for data in Path(appdata, "MetaQuotes", "Terminal").glob("*"):
            if (origin := _origin(data)) is not None:
                candidates.append(origin / EXE)

    running = running_executables()
    found: dict[str, dict] = {}
    for exe in candidates:
        key = _same(str(exe))
        if key in found or not exe.is_file():
            continue
        found[key] = {"path": str(exe), "name": exe.parent.name, "running": key in running}
    return sorted(found.values(), key=lambda t: (not t["running"], t["name"].lower()))


def running_executables() -> set[str]:
    """Normalised paths of every running program this user can see (Windows only)."""
    if sys.platform != "win32":
        return set()
    import ctypes
    from ctypes import wintypes

    psapi, kernel32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    pids = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return set()
    out = set()
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                out.add(_same(buf.value))
        finally:
            kernel32.CloseHandle(handle)
    return out


def is_running(path: str) -> bool:
    """Whether the terminal at ``path`` is open. True off Windows, where it can't be checked."""
    if sys.platform != "win32":
        return True
    return _same(path) in running_executables()
