"""Drawing templates: named styles the trader saves from a drawing and applies to another.

A template holds a drawing's look, not its place: colour, line width and style, the text size
and the label (a line's "H1 OB", a text's words). Templates are kept per family, since a
rectangle's style means little on a text: lines (trendline, horizontal line, path), rectangles
and texts. They live in the settings table, so they follow the database, not the browser.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .drawings import DASHES, MAX_TEXT

TEMPLATES_KEY = "drawing_templates"  # family -> name -> {"style", "text", "saved_at"}
FAMILIES = {"trendline": "line", "hline": "line", "path": "line", "rect": "rect", "text": "text"}
MAX_TEMPLATES = 100  # per family
_COLOR_CHARS = set("0123456789abcdefABCDEF")


class TemplateError(ValueError):
    """Something the trader can fix, with a message for them."""


def clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise TemplateError("Give the template a name, e.g. H1 OB")
    return name


def clean_family(family: str) -> str:
    if family not in set(FAMILIES.values()):
        raise TemplateError(f"Unknown drawing family {family!r}: line, rect or text")
    return family


def clean_template(body: dict) -> dict:
    """A template as stored, from the dashboard's JSON; TemplateError if it isn't one."""
    style = body.get("style")
    if not isinstance(style, dict):
        raise TemplateError("A template needs a style")
    color = style.get("color")
    if color is not None and not (isinstance(color, str) and len(color) == 7 and color[0] == "#"
                                  and set(color[1:]) <= _COLOR_CHARS):
        raise TemplateError("style.color must be #rrggbb or null")
    width = style.get("width", 1)
    if isinstance(width, bool) or not isinstance(width, (int, float)) or not 1 <= width <= 4:
        raise TemplateError("style.width must be 1 to 4")
    dash = style.get("dash", "solid")
    if dash not in DASHES:
        raise TemplateError(f"style.dash must be one of {', '.join(DASHES)}")
    size = style.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, (int, float)) or not 8 <= size <= 48):
        raise TemplateError("style.size must be 8 to 48")
    text = body.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
        raise TemplateError(f"text must be at most {MAX_TEXT} characters")
    return {
        "style": {"color": color, "width": width, "dash": dash, **({"size": size} if size is not None else {})},
        "text": text.strip() or None if isinstance(text, str) else None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


class DrawingTemplates:
    """The saved templates, read and written through the store's settings."""

    def __init__(self, store) -> None:
        self.store = store

    def _all(self) -> dict:
        return self.store.get_setting(TEMPLATES_KEY) or {}

    def list(self) -> list[dict]:
        """Every template, by family then name (case-insensitive)."""
        out = [{"family": fam, "name": name, **t} for fam, named in self._all().items() for name, t in named.items()]
        return sorted(out, key=lambda t: (t["family"], t["name"].casefold()))

    def save(self, family: str, name: str, body: dict) -> dict:
        family, name = clean_family(family), clean_name(name)
        template = clean_template(body)
        everything = self._all()
        named = everything.setdefault(family, {})
        if name not in named and len(named) >= MAX_TEMPLATES:
            raise TemplateError(f"At most {MAX_TEMPLATES} templates per drawing type: delete one first")
        named[name] = template
        self.store.set_setting(TEMPLATES_KEY, everything)
        return {"family": family, "name": name, **template}

    def delete(self, family: str, name: str) -> bool:
        everything = self._all()
        if name not in everything.get(family, {}):
            return False
        del everything[family][name]
        self.store.set_setting(TEMPLATES_KEY, everything)
        return True
