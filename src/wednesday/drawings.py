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
# The tools of web/src/shapes.ts: a fixed number of clicks, a polyline (2 to 200) or a brush stroke (2 to 2000).
POINTS.update({
    "ray": (2, 2), "extendedLine": (2, 2), "infoLine": (2, 2), "trendAngle": (2, 2),
    "horizontalRay": (1, 1), "verticalLine": (1, 1), "crossLine": (1, 1), "parallelChannel": (3, 3),
    "regression": (2, 2), "disjointChannel": (3, 3), "flatTopBottom": (3, 3),
    "fibRetracement": (2, 2), "fibExtension": (3, 3), "fibChannel": (3, 3), "fibTimeZones": (2, 2),
    "fibFan": (2, 2), "fibArcs": (2, 2), "fibCircles": (2, 2), "fibSpiral": (2, 2),
    "fibWedge": (3, 3), "pitchfork": (3, 3), "schiffPitchfork": (3, 3), "modSchiffFork": (3, 3),
    "gannFan": (2, 2), "gannBox": (2, 2), "gannSquare": (2, 2), "pitchfan": (3, 3),
    "cyclicLines": (2, 2), "timeCycles": (2, 2), "sineLine": (2, 2), "xabcd": (5, 5), "abcd": (4, 4),
    "headShoulders": (7, 7), "cypher": (5, 5), "threeDrives": (7, 7), "elliottWave": (6, 6),
    "elliottImpulse": (6, 6), "elliottCorrect": (4, 4), "elliottTriangle": (6, 6),
    "elliottDouble": (4, 4), "elliottTriple": (6, 6), "forecast": (2, 2), "projection": (3, 3),
    "barsPattern": (2, 2), "priceRange": (2, 2), "dateRange": (2, 2), "datePriceRange": (2, 2),
    "measure": (2, 2), "rotatedRect": (3, 3), "circle": (2, 2), "ellipse": (2, 2), "triangle": (3, 3),
    "polyline": (2, 200), "curve": (3, 3), "arc": (3, 3), "brush": (2, 2000),
    "highlighter": (2, 2000), "priceLabel": (1, 1), "arrow": (2, 2), "note": (1, 1), "callout": (2, 2), "flag": (1, 1),
    "arrowMark": (1, 1), "icon": (1, 1), "signpost": (1, 1), "balloon": (1, 1),
    "anchoredText": (1, 1),
    "anchoredNote": (1, 1),
})
# Tools anchored to the screen: props.ax and props.ay, the place as a share of the chart's width and height.
ANCHORED = ("anchoredText", "anchoredNote")
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
        # Lines, rectangles and paths may carry a short label ("H1 OB"); an empty one is dropped.
        label = (props or {}).get("text")
        if label is not None and (not isinstance(label, str) or len(label) > MAX_TEXT):
            raise ValueError(f"A label (props.text) must be text, at most {MAX_TEXT} characters")
        props = {"text": label} if label and label.strip() else None
        if kind in ANCHORED:
            props = {**(props or {}), **_anchor(body.get("props") or {})}
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


def _anchor(props: dict) -> dict:
    """An anchored tool's place on the chart, as shares of its width and height (0 to 1)."""
    out = {}
    for key in ("ax", "ay"):
        v = props.get(key, 0.1)
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"props.{key} must be a number from 0 to 1")
        out[key] = min(1.0, max(0.0, float(v)))
    return out


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
