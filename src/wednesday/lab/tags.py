"""What can be labelled, and how detector levels map onto those tags."""

from __future__ import annotations

from ..levels import Level

# id -> (title, detector, detector kind, drawn as)
TAGS: dict[str, dict] = {
    "ob_bull": {"title": "OB bull", "detector": "ob", "kind": "bullish", "shape": "body"},
    "ob_bear": {"title": "OB bear", "detector": "ob", "kind": "bearish", "shape": "body"},
    "bsl": {"title": "BSL", "detector": "liquidity", "kind": "bsl", "shape": "high"},
    "ssl": {"title": "SSL", "detector": "liquidity", "kind": "ssl", "shape": "low"},
    "idm_bull": {"title": "IDM bull", "detector": "idm", "kind": "bullish", "shape": "low"},
    "idm_bear": {"title": "IDM bear", "detector": "idm", "kind": "bearish", "shape": "high"},
}
OB_TAGS = ("ob_bull", "ob_bear")


def tag_of(level: Level) -> str | None:
    return next((t for t, info in TAGS.items() if info["detector"] == level.detector and info["kind"] == level.kind), None)


def catalog() -> list[dict]:
    return [{"id": t, "title": info["title"], "shape": info["shape"]} for t, info in TAGS.items()]
