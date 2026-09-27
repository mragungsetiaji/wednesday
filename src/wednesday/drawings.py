"""Chart drawings the trader makes (trendlines, lines, rectangles, positions): validation.

Stored per source and symbol (see ``storage.drawings_table``). Points are (time, price) with
time in unix seconds of the feed's clock, the same axis as the candles.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .storage import DRAWING_KINDS

# Anchor points per kind, as (fewest, most): a path takes any number, the rest a fixed count.
POINTS = {"trendline": (2, 2), "hline": (1, 1), "rect": (2, 2), "long": (2, 2), "short": (2, 2),
          "path": (2, 200), "text": (1, 1)}
DASHES = ("solid", "dashed", "dotted")
MAX_TEXT = 500
# A long or short position: its two points are the start and end at the entry price, and
# props hold the stop and target prices.
POSITIONS = ("long", "short")
_ID = re.compile(r"^[A-Za-z0-9-]{8,36}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_drawing(drawing_id: str, body: dict) -> dict:
    """A storable row from the dashboard's JSON (without source and symbol); ValueError if invalid."""
    if not _ID.match(drawing_id):
        raise ValueError("Bad drawing id")
    kind = body.get("kind")
    if kind not in DRAWING_KINDS:
        raise ValueError(f"Unknown drawing kind {kind!r}")
    raw = body.get("points")
    low, high = POINTS[kind]
    if not isinstance(raw, list) or not low <= len(raw) <= high:
        raise ValueError(f"A {kind} takes {low} point(s)" if low == high else f"A {kind} takes {low} to {high} points")
    points = []
    for pt in raw:
        try:
            t, p = int(pt["t"]), float(pt["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Each point needs a time t and a price p") from exc
        if not math.isfinite(p):
            raise ValueError("A point's price must be a number")
        points.append({"t": t, "p": p})
    props = body.get("props") if isinstance(body.get("props"), dict) else None
    if kind in POSITIONS:
        props = _position_props(kind, points, props)
    elif kind == "text":
        text = (props or {}).get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError(f"A text needs props.text, 1 to {MAX_TEXT} characters")
        props = {"text": text}
    else:
        props = None
    style = body.get("style") or {}
    if not isinstance(style, dict):
        raise ValueError("style must be an object")
    color = style.get("color")
    if color is not None and not (isinstance(color, str) and _COLOR.match(color)):
        raise ValueError("style.color must be #rrggbb or null")
    width = style.get("width", 1)
    if not isinstance(width, (int, float)) or not 1 <= width <= 4:
        raise ValueError("style.width must be 1 to 4")
    dash = style.get("dash", "solid")
    if dash not in DASHES:
        raise ValueError(f"style.dash must be one of {', '.join(DASHES)}")
    size = style.get("size")
    if size is not None and not (isinstance(size, (int, float)) and 8 <= size <= 48):
        raise ValueError("style.size (text) must be 8 to 48")
    timeframes = body.get("timeframes")
    if timeframes is not None and not (isinstance(timeframes, list) and all(isinstance(t, str) for t in timeframes)):
        raise ValueError("timeframes must be a list of names or null")
    return {
        "id": drawing_id,
        "kind": kind,
        "points": points,
        "style": {"color": color, "width": width, "dash": dash, **({"size": size} if size is not None else {})},
        "props": props,
        "timeframes": timeframes,
        "locked": bool(body.get("locked", False)),
        "hidden": bool(body.get("hidden", False)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _position_props(kind: str, points: list[dict], props: dict | None) -> dict:
    """Stop and target of a position, each on its own side of the entry."""
    try:
        stop, target = float((props or {})["stop"]), float((props or {})["target"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("A position needs props.stop and props.target prices") from exc
    if not (math.isfinite(stop) and math.isfinite(target)):
        raise ValueError("A position's stop and target must be numbers")
    entry = points[0]["p"]
    points[1]["p"] = entry  # both points sit at the entry; the second one only marks the end
    below, above = (stop, target) if kind == "long" else (target, stop)
    if not below < entry < above:
        side = "below" if kind == "long" else "above"
        raise ValueError(f"A {kind} position's stop must be {side} the entry and its target on the other side")
    return {"stop": stop, "target": target}


def drawing_payload(row: dict) -> dict:
    """The API shape: a stored row without the source and symbol it's filed under."""
    return {k: v for k, v in row.items() if k not in ("source", "symbol")}
