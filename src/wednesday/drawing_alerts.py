"""Price alerts on the trader's own drawings: a horizontal line or trendline crossed, a
rectangle entered or left.

An alert belongs to a drawing (``storage.drawing_alerts_table``, keyed by the drawing's id)
and reads the drawing's points each time it is checked, so moving the drawing moves the
alert. It is checked after every scan on the closed M1 bars that arrived since the last
check, only bars after the one that closed last when it was armed:

* line, crossing up: the bar's high reaches the line while the close before it was below;
  crossing down the other way round; ``cross`` either. A trendline's price is taken at
  the bar's time, on the line through its two points (extended to the right, never before
  its first point).
* rectangle, entering: the close before it was outside the box's prices and the bar's
  range reaches into them; leaving: the close before it was inside and the bar closes
  outside. Only bars from the rectangle's left edge on count.

``once`` fires on the first such bar and disarms (the drawing's bell greys out until it is
re-armed); ``every`` fires on each one, at most once per bar. An alert can expire, and can
also be checked on the live tick (``intrabar``): the price moving through the line or into
or out of the box between two ticks, labelled intrabar since the bar hasn't closed.

Each firing is logged in ``alert_log`` under its own key before anything else, so a bar
never fires twice, even across restarts. Messages state what happened, never advice.
"""

from __future__ import annotations

import html
import logging
import math
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)

CONDITIONS = {
    "hline": ("cross_up", "cross_down", "cross"),
    "trendline": ("cross_up", "cross_down", "cross"),
    "rect": ("enter", "exit"),
}
MODES = ("once", "every")
MAX_NOTE = 200
CONDITION_TEXT = {"cross_up": "crossing up", "cross_down": "crossing down", "cross": "crossing",
                  "enter": "entering", "exit": "leaving"}


def clean_alert(kind: str, body: dict, armed_bar: int | None) -> dict:
    """A storable alert for a drawing of ``kind`` from the dashboard's JSON; ValueError if invalid.
    ``armed_bar``: the feed-clock unix time of the last closed M1 bar now (only later bars count)."""
    if kind not in CONDITIONS:
        raise ValueError("Alerts work on horizontal lines, trendlines and rectangles")
    condition = body.get("condition")
    if condition not in CONDITIONS[kind]:
        raise ValueError(f"A {kind} alert's condition is one of {', '.join(CONDITIONS[kind])}")
    mode = body.get("mode", "once")
    if mode not in MODES:
        raise ValueError(f"mode is one of {', '.join(MODES)}")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise ValueError(f"The note is text of up to {MAX_NOTE} characters")
    expires = body.get("expires_at")
    if expires is not None:
        try:
            ts = pd.Timestamp(expires)
        except (TypeError, ValueError) as exc:
            raise ValueError("expires_at is an ISO date and time, or null") from exc
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        if ts <= pd.Timestamp.now(tz="UTC"):
            raise ValueError("The expiry is in the past")
        expires = ts.isoformat()
    return {
        "condition": condition,
        "mode": mode,
        "note": (note or "").strip() or None,
        "expires_at": expires,
        "intrabar": bool(body.get("intrabar", False)),
        "armed": True,
        "armed_bar": armed_bar,
        "fired_at": None,
        "fired_price": None,
        "fires": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def line_price(drawing: dict, t: float) -> float | None:
    """The price of a line drawing at feed-clock time ``t``; None before a trendline's first point."""
    pts = drawing["points"]
    if drawing["kind"] == "hline":
        return float(pts[0]["p"])
    a, b = sorted(pts[:2], key=lambda p: p["t"])
    if t < a["t"]:
        return None
    if b["t"] == a["t"]:
        return float(b["p"])
    return float(a["p"] + (b["p"] - a["p"]) * (t - a["t"]) / (b["t"] - a["t"]))


def _box(drawing: dict) -> tuple[float, float, int]:
    ps = [p["p"] for p in drawing["points"]]
    return min(ps), max(ps), min(p["t"] for p in drawing["points"])


def bar_events(drawing: dict, alert: dict, bars: pd.DataFrame, prev_close: float | None) -> list[tuple[int, float, str]]:
    """(bar unix, level, what) for each closed bar in ``bars`` meeting the alert's condition.
    ``prev_close``: the close of the bar before the first one (None: the first bar only sets it)."""
    out = []
    cond = alert["condition"]
    times = (pd.DatetimeIndex(bars.index).as_unit("s").asi8).tolist()
    for t, hi, lo, close in zip(times, bars["high"].to_numpy(float), bars["low"].to_numpy(float), bars["close"].to_numpy(float)):
        if prev_close is not None:
            if drawing["kind"] == "rect":
                bottom, top, left = _box(drawing)
                inside = bottom <= prev_close <= top
                if t >= left:
                    if cond == "enter" and not inside and hi >= bottom and lo <= top:
                        out.append((t, top if prev_close > top else bottom, "entered"))
                    elif cond == "exit" and inside and not bottom <= close <= top:
                        out.append((t, top if close > top else bottom, "left"))
            else:
                level = line_price(drawing, t)
                if level is not None:
                    if cond in ("cross_up", "cross") and prev_close < level <= hi:
                        out.append((t, level, "crossed above"))
                    elif cond in ("cross_down", "cross") and prev_close > level >= lo:
                        out.append((t, level, "crossed below"))
        prev_close = close
    return out


def tick_event(drawing: dict, alert: dict, before: float, now: float, t: float) -> tuple[float, str] | None:
    """(level, what) when the price moving from ``before`` to ``now`` meets the condition."""
    cond = alert["condition"]
    if drawing["kind"] == "rect":
        bottom, top, left = _box(drawing)
        if t < left:
            return None
        was, is_ = bottom <= before <= top, bottom <= now <= top
        if cond == "enter" and not was and is_:
            return (top if before > top else bottom), "entered"
        if cond == "exit" and was and not is_:
            return (top if now > top else bottom), "left"
        return None
    level = line_price(drawing, t)
    if level is None:
        return None
    if cond in ("cross_up", "cross") and before < level <= now:
        return level, "crossed above"
    if cond in ("cross_down", "cross") and before > level >= now:
        return level, "crossed below"
    return None


def describe(drawing: dict) -> str:
    return {"hline": "line", "trendline": "trendline", "rect": "zone"}.get(drawing["kind"], drawing["kind"])


def format_message(symbol: str, drawing: dict, alert: dict, level: float, what: str, price: float,
                   bar_time: str | None, intrabar: bool, digits: int = 2) -> str:
    f = lambda v: f"{v:,.{digits}f}"  # noqa: E731
    name = f" '{html.escape(alert['note'])}'" if alert.get("note") else ""
    if drawing["kind"] == "rect":
        bottom, top, _ = _box(drawing)
        head = f"<b>{html.escape(symbol)} {what} your zone{name}</b> {f(bottom)} – {f(top)}"
    else:
        head = f"<b>{html.escape(symbol)} {what} {f(level)}</b>, your {describe(drawing)}{name}"
    when = "Intrabar (live tick, the bar hasn't closed)" if intrabar else f"On the closed M1 bar of {bar_time} (chart time)"
    lines = [head, f"Price {f(price)} · {when}"]
    if alert["mode"] == "once":
        lines.append("The alert is now off; re-arm it on the chart.")
    return "\n".join(lines)


class DrawingAlerts:
    """Checks the armed drawing alerts of the running source and symbol and sends what fires."""

    def __init__(self, store, send: Callable[[str], None] | None = None):
        self.store = store
        self.send = send  # sends a Telegram message (HTML); None or failing: logged only
        self._last_bar: dict[tuple[str, str], pd.Timestamp] = {}
        self._last_tick: dict[tuple[str, str], float] = {}
        self._cache: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
        self.last_error: str | None = None

    def invalidate(self) -> None:
        """A drawing or an alert changed: read them again on the next check."""
        self._cache.clear()

    def _armed(self, source: str, symbol: str) -> list[tuple[dict, dict]]:
        if self.store is None:
            return []
        cached = self._cache.get((source, symbol))
        if cached is None:
            cached = self._cache[(source, symbol)] = self._load(source, symbol)
        now = pd.Timestamp.now(tz="UTC")
        out = []
        for d, a in cached:
            if not a["armed"]:
                continue
            if a["expires_at"] and pd.Timestamp(a["expires_at"]) <= now:
                a["armed"] = False
                self.store.drawing_alert_update(a["drawing_id"], {"armed": False})
                continue
            out.append((d, a))
        return out

    def _load(self, source: str, symbol: str) -> list[tuple[dict, dict]]:
        drawings = {d["id"]: d for d in self.store.drawings(source, symbol)}
        out = []
        for a in self.store.drawing_alerts(source, symbol):
            d = drawings.get(a["drawing_id"])
            if a["armed"] and d is not None and d["kind"] in CONDITIONS:  # a deleted drawing keeps its alert, for undo
                out.append((d, a))
        return out

    def check(self, source: str, symbol: str, m1: pd.DataFrame, send: Callable[[str], None] | None = None) -> list[str]:
        """Alerts fired by the closed M1 bars since the last check. Returns the keys logged."""
        if m1 is None or m1.empty:
            return []
        stream = (source, symbol)
        last = self._last_bar.get(stream)
        self._last_bar[stream] = m1.index[-1]
        if last is None:
            return []  # first scan of this stream: only bars that arrive from now on
        new = m1[m1.index > last]
        if new.empty:
            return []
        prev_close = float(m1[m1.index <= last]["close"].iloc[-1]) if (m1.index <= last).any() else None
        fired = []
        for d, a in self._armed(source, symbol):
            bars, before = new, prev_close
            if a["armed_bar"] is not None:
                after = pd.DatetimeIndex(new.index).as_unit("s").asi8 > a["armed_bar"]
                if not after.any():
                    continue
                first = int(after.argmax())
                before = float(new["close"].iloc[first - 1]) if first > 0 else prev_close
                bars = new[after]
            for t, level, what in bar_events(d, a, bars, before):
                bar_close = float(bars.loc[pd.Timestamp(t, unit="s"), "close"])
                bar_time = pd.Timestamp(t, unit="s").strftime("%Y-%m-%d %H:%M")
                key = self._fire(source, symbol, d, a, level, what, bar_close, f"bar{t}", bar_time, False, send)
                if key:
                    fired.append(key)
                    if a["mode"] == "once":
                        break
        return fired

    def check_tick(self, source: str, symbol: str, price: float, at: pd.Timestamp,
                   send: Callable[[str], None] | None = None) -> list[str]:
        """Intrabar alerts: the live price moved through a line or into / out of a box since the last tick."""
        stream = (source, symbol)
        before = self._last_tick.get(stream)
        self._last_tick[stream] = price
        if before is None or before == price or not math.isfinite(price):
            return []
        t = int(pd.Timestamp(at).timestamp())
        fired = []
        for d, a in self._armed(source, symbol):
            if not a["intrabar"]:
                continue
            hit = tick_event(d, a, before, price, t)
            if hit:
                level, what = hit
                key = self._fire(source, symbol, d, a, level, what, price, f"tick{t // 60}", None, True, send)
                if key:
                    fired.append(key)
        return fired

    def _fire(self, source, symbol, d, a, level, what, price, when_key, bar_time, intrabar, send) -> str | None:
        base = f"drawing|{source}|{symbol}|{d['id']}|"
        key = base + when_key
        if self.store.alert_logged(key):
            return None  # this bar (or minute of ticks) already fired it
        if when_key.startswith("bar") and self.store.alert_logged(f"{base}tick{int(when_key[3:]) // 60}"):
            return None  # a live tick in this minute fired it already
        send = send or self.send
        row = {"key": key, "source": source, "symbol": symbol, "timeframe": "tick" if intrabar else "M1",
               "kind": "drawing", "priority": a["condition"], "entry": float(level), "price": float(price),
               "sent_at": datetime.now(timezone.utc).isoformat(),
               "summary": f"{what.capitalize()} your {describe(d)}" + (f" '{a['note']}'" if a.get("note") else "")
                          + (" (intrabar)" if intrabar else "")}
        status, error = "logged", None
        if send is not None:
            try:
                send(format_message(symbol, d, a, level, what, price, bar_time, intrabar))
                status = "sent"
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - a failed message must not stop the scan
                status, error = "failed", str(exc)
                self.last_error = error
                log.warning("drawing alert not sent: %s", exc)
        self.store.log_alert({**row, "status": status, "error": error})
        patch = {"fired_at": row["sent_at"], "fired_price": float(price), "fires": int(a["fires"] or 0) + 1}
        if a["mode"] == "once":
            patch["armed"] = False
        a.update(patch)
        self.store.drawing_alert_update(d["id"], patch)
        return key
