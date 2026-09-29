"""Chart snapshots kept for the journal: PNG files under ``data/snapshots``.

The dashboard makes the image (the chart, its drawings and a footer with the symbol,
timeframe, time and the TradingView notice) and uploads it; the server only checks it
is a PNG of reasonable size and stores it under a random id. Files never leave the
snapshot folder: an id is 32 hex characters, nothing else is accepted.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

SNAPSHOT_DIR = Path("data/snapshots")
MAX_BYTES = 12 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ID = re.compile(r"^[0-9a-f]{32}$")


class SnapshotError(ValueError):
    pass


class Snapshots:
    def __init__(self, root: str | Path = SNAPSHOT_DIR):
        self.root = Path(root)

    def save(self, data: bytes) -> str:
        if len(data) > MAX_BYTES:
            raise SnapshotError("The image is larger than 12 MB")
        if not data.startswith(PNG_SIGNATURE):
            raise SnapshotError("A snapshot is a PNG image")
        self.root.mkdir(parents=True, exist_ok=True)
        sid = uuid.uuid4().hex
        (self.root / f"{sid}.png").write_bytes(data)
        return sid

    def path(self, sid: str) -> Path | None:
        """The file of snapshot ``sid``, or None (unknown, or not an id at all)."""
        if not _ID.match(sid or ""):
            return None
        p = self.root / f"{sid}.png"
        return p if p.is_file() else None

    def delete(self, sid: str) -> None:
        p = self.path(sid)
        if p:
            p.unlink(missing_ok=True)

    @staticmethod
    def valid(sid: str) -> bool:
        return bool(_ID.match(sid or ""))
