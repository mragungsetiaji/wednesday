"""Reference data for the drawings and their price alerts (#96, step 9e): what ``drawings.py`` and
``drawing_alerts.py`` give for many inputs, which the Rust port (``wed-server/tests/it/drawings_golden.rs``)
must give too.

``tests/golden/drawings/golden.json`` holds: ``clean_drawing`` and ``clean_alert`` inputs with their
row or error (without the time they were made), ``bar_events`` and ``tick_event`` scenarios for each
kind and condition, ``format_message`` texts, and full ``DrawingAlerts`` runs over M1 bars and ticks
on a SQLite store, with the keys fired, the messages sent and the rows left in the database.

    UPDATE_GOLDEN=1 uv run pytest tests/test_drawings_golden.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from wednesday.drawing_alerts import DrawingAlerts, bar_events, clean_alert, format_message, tick_event
from wednesday.drawings import clean_drawing
from wednesday.storage import Store

GOLDEN = Path(__file__).with_name("golden") / "drawings" / "golden.json"
ID = "d1a2b3c4-0000-4000-8000-000000000001"
T0 = 1_767_261_600  # 2026-01-01 10:00 on the feed's clock


def _pt(t, p):
    return {"t": t, "p": p}


# ---- clean_drawing ----

DRAWING_CASES: list[tuple[str, object]] = [
    (ID, {"kind": "hline", "points": [_pt(T0, 2000.5)]}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"color": "#AABBcc", "width": 2, "dash": "dashed"},
          "props": {"text": "  Asia high "}, "timeframes": ["H1", "M15"], "locked": 1, "hidden": "yes"}),
    (ID, {"kind": "trendline", "points": [_pt(T0, 2000.25), _pt(T0 + 3600, 2010.75)], "style": {"width": 1.5}}),
    (ID, {"kind": "trendline", "points": [_pt(T0, 1)]}),
    (ID, {"kind": "trendline", "points": "no"}),
    (ID, {"kind": "rect", "points": [_pt("1767261600", "2001.5"), _pt(T0 + 60.9, " 1999 ")],
          "style": {"width": True, "size": 12}}),
    (ID, {"kind": "rect", "points": [_pt(T0, 2000), {"t": T0}]}),
    (ID, {"kind": "rect", "points": [_pt(T0, 2000), [T0, 2000]]}),
    (ID, {"kind": "rect", "points": [_pt(T0, 2000), _pt("1.5", 2000)]}),
    (ID, {"kind": "rect", "points": [_pt(T0, 2000), _pt(T0, "nan")]}),
    (ID, {"kind": "rect", "points": [_pt(T0, 2000), _pt(T0, "1_000.5")]}),
    (ID, {"kind": "rect", "points": [_pt(T0, 2000), _pt(T0, None)]}),
    (ID, {"kind": "long", "points": [_pt(T0, 2000), _pt(T0 + 600, 2003)], "props": {"stop": 1995, "target": "2010.5"}}),
    (ID, {"kind": "long", "points": [_pt(T0, 2000), _pt(T0 + 600, 2003)], "props": {"stop": 2005, "target": 2010}}),
    (ID, {"kind": "short", "points": [_pt(T0, 2000), _pt(T0 + 600, 2003)], "props": {"stop": 2005, "target": 1990}}),
    (ID, {"kind": "short", "points": [_pt(T0, 2000), _pt(T0 + 600, 2003)], "props": {"stop": 1995, "target": 1990}}),
    (ID, {"kind": "short", "points": [_pt(T0, 2000), _pt(T0 + 600, 2003)], "props": {"stop": 2005}}),
    (ID, {"kind": "short", "points": [_pt(T0, 2000), _pt(T0 + 600, 2003)], "props": {"stop": "inf", "target": 1990}}),
    (ID, {"kind": "long", "points": [_pt(T0, 2000), _pt(T0 + 600, 2003)], "props": "x"}),
    (ID, {"kind": "text", "points": [_pt(T0, 2000)], "props": {"text": "Note <b>&</b> é"}, "style": {"size": 48}}),
    (ID, {"kind": "text", "points": [_pt(T0, 2000)], "props": {"text": "   "}}),
    (ID, {"kind": "text", "points": [_pt(T0, 2000)], "props": {"text": "x" * 501}}),
    (ID, {"kind": "text", "points": [_pt(T0, 2000)]}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "props": {"text": 5}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "props": {"text": ""}}),
    (ID, {"kind": "path", "points": [_pt(T0 + i * 60, 2000 + i) for i in range(5)]}),
    (ID, {"kind": "path", "points": [_pt(T0, 2000)]}),
    (ID, {"kind": "brush", "points": [_pt(T0, 2000)] * 2001}),
    (ID, {"kind": "anchoredText", "points": [_pt(T0, 2000)], "props": {"text": "hi", "ax": 1.5, "ay": -2}}),
    (ID, {"kind": "anchoredNote", "points": [_pt(T0, 2000)]}),
    (ID, {"kind": "anchoredNote", "points": [_pt(T0, 2000)], "props": {"ax": "0.5"}}),
    (ID, {"kind": "anchoredNote", "points": [_pt(T0, 2000)], "props": {"ay": True}}),
    (ID, {"kind": "parallelChannel", "points": [_pt(T0, 1), _pt(T0, 2), _pt(T0, 3)]}),
    (ID, {"kind": "xabcd", "points": [_pt(T0, 1)] * 4}),
    (ID, {"kind": "circle"}),
    (ID, {"kind": "nope", "points": []}),
    (ID, {"kind": None}),
    (ID, {}),
    (ID, {"kind": ["hline"]}),
    (ID, {"kind": "it's", "points": []}),
    (ID, {"kind": 3.5}),
    (ID, {"kind": {"a": [1, None, True]}}),
    ("short", {"kind": "hline", "points": [_pt(T0, 2000)]}),
    ("bad_id!!", {"kind": "hline", "points": [_pt(T0, 2000)]}),
    ("abcdefgh\n", {"kind": "hline", "points": [_pt(T0, 2000)]}),
    ("a" * 36, {"kind": "hline", "points": [_pt(T0, 2000)]}),
    ("a" * 37, {"kind": "hline", "points": [_pt(T0, 2000)]}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": "red"}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": []}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"color": "red"}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"color": "#aabbcc\n"}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"color": None, "width": 5}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"width": "2"}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"width": 4.0, "dash": "dotted"}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"dash": "wavy"}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"size": 7}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "style": {"size": 8.5}}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "timeframes": "H1"}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "timeframes": ["H1", 4]}),
    (ID, {"kind": "hline", "points": [_pt(T0, 2000)], "timeframes": [], "locked": 0, "hidden": [0]}),
]


def drawing_cases() -> list[dict]:
    out = []
    for drawing_id, body in DRAWING_CASES:
        try:
            row = clean_drawing(drawing_id, body)
            row.pop("updated_at")
            out.append({"id": drawing_id, "body": body, "out": row})
        except ValueError as exc:
            out.append({"id": drawing_id, "body": body, "error": str(exc)})
    return out


# ---- clean_alert ----

ALERT_CASES: list[tuple[str, object, int | None]] = [
    ("hline", {"condition": "cross_up"}, T0),
    ("hline", {"condition": "cross", "mode": "every", "note": "  Asia <high> ", "intrabar": 1}, None),
    ("hline", {"condition": "cross_down", "note": "   "}, T0),
    ("trendline", {"condition": "cross_down", "expires_at": "2099-06-01T10:00:00+07:00"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": "2099-06-01"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": "2099-06-01 10:30"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": "2099-06-01T10:30:00.5Z"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": "2099-06-01T10:30:00.123456789Z"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": "2099-06-01T10:30:00-0330"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": "2001-06-01T10:00:00"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": "tomorrow-ish"}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": 5}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": True}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": [1]}, T0),
    ("trendline", {"condition": "cross_up", "expires_at": None}, T0),
    ("rect", {"condition": "enter"}, T0),
    ("rect", {"condition": "exit", "mode": "every"}, T0),
    ("rect", {"condition": "cross"}, T0),
    ("hline", {"condition": "enter"}, T0),
    ("hline", {}, T0),
    ("hline", {"condition": "cross", "mode": "twice"}, T0),
    ("hline", {"condition": "cross", "mode": None}, T0),
    ("hline", {"condition": "cross", "note": 5}, T0),
    ("hline", {"condition": "cross", "note": "n" * 201}, T0),
    ("hline", {"condition": "cross", "note": "n" * 200}, T0),
    ("text", {"condition": "cross"}, T0),
    ("long", {"condition": "cross"}, T0),
]


def alert_cases() -> list[dict]:
    out = []
    for kind, body, armed_bar in ALERT_CASES:
        try:
            row = clean_alert(kind, body, armed_bar)
            row.pop("created_at")
            out.append({"kind": kind, "body": body, "armed_bar": armed_bar, "out": row})
        except ValueError as exc:
            out.append({"kind": kind, "body": body, "armed_bar": armed_bar, "error": str(exc)})
    return out


# ---- bar_events, tick_event, format_message ----

HLINE = {"id": ID, "kind": "hline", "points": [_pt(T0, 2000.0)]}
TREND = {"id": ID, "kind": "trendline", "points": [_pt(T0 + 600, 2010.0), _pt(T0, 2000.0)]}
FLAT = {"id": ID, "kind": "trendline", "points": [_pt(T0, 2000.0), _pt(T0, 2003.0)]}
RECT = {"id": ID, "kind": "rect", "points": [_pt(T0 + 120, 2005.0), _pt(T0 + 900, 1995.0)]}
SHAPES = {"hline": HLINE, "trendline": TREND, "flat": FLAT, "rect": RECT}
CONDS = {"hline": ("cross_up", "cross_down", "cross"), "trendline": ("cross_up", "cross_down", "cross"),
         "flat": ("cross_up", "cross_down", "cross"), "rect": ("enter", "exit")}

# A walk through the levels, one bar a minute from T0 - 120: [high, low, close].
WALK = [(1999, 1990, 1995), (2001, 1996, 1999), (2003, 1998, 2002), (2004.5, 1999.5, 2004), (2008, 2003, 2007),
        (2009, 2000.0, 2001), (2002, 1993, 1994), (1997.5, 1992, 1997), (2006, 1996, 2006), (2010, 2004, 2009),
        (2012.25, 2008, 2011), (2011, 2004.4, 2005), (2006, 1990, 1991), (1996, 1990, 1995.5), (2000, 1995, 1999.99)]


def walk_frame(start: int = T0 - 120) -> pd.DataFrame:
    index = pd.to_datetime([start + 60 * i for i in range(len(WALK))], unit="s")
    return pd.DataFrame({"open": [c for _, _, c in WALK], "high": [h for h, _, _ in WALK],
                         "low": [lo for _, lo, _ in WALK], "close": [c for _, _, c in WALK],
                         "volume": 1.0}, index=index).astype(float)


def bar_cases() -> list[dict]:
    out = []
    bars = walk_frame()
    rows = [[int(t.timestamp()), float(r.high), float(r.low), float(r.close)] for t, r in bars.iterrows()]
    for name, d in SHAPES.items():
        for cond in CONDS[name]:
            for prev in (None, 1990.0, 2001.0):
                ev = bar_events(d, {"condition": cond}, bars, prev)
                out.append({"drawing": d, "condition": cond, "prev_close": prev, "bars": rows,
                            "out": [[t, float(level), what] for t, level, what in ev]})
    return out


TICKS = [(1999.0, 2000.0), (2000.0, 1999.0), (1998.0, 2001.5), (2002.0, 1990.0), (2006.0, 2004.0), (2004.0, 2006.0),
         (1990.0, 1995.0), (1995.0, 1994.9), (2005.0, 2005.0), (2000.5, 2003.25)]


def tick_cases() -> list[dict]:
    out = []
    for name, d in SHAPES.items():
        for cond in CONDS[name]:
            for t in (T0 - 60, T0 + 300, T0 + 1200):
                for before, now in TICKS:
                    hit = tick_event(d, {"condition": cond}, before, now, t)
                    out.append({"drawing": d, "condition": cond, "before": before, "now": now, "t": t,
                                "out": None if hit is None else [float(hit[0]), hit[1]]})
    return out


def message_cases() -> list[dict]:
    out = []
    big = {"id": ID, "kind": "rect", "points": [_pt(T0, 12345.125), _pt(T0, 0.125)]}
    for d in (HLINE, TREND, RECT, big):
        for note in (None, "Asia <high> & \"x\" 'y'"):
            for mode in ("once", "every"):
                for intrabar in (False, True):
                    a = {"note": note, "mode": mode, "condition": "cross"}
                    for level, price in ((2000.0, 2001.005), (1234567.875, -0.004), (2.675, 1e-7)):
                        msg = format_message("FX:XAU<USD>", d, a, level, "crossed above", price,
                                             None if intrabar else "2026-01-01 10:05", intrabar)
                        out.append({"symbol": "FX:XAU<USD>", "drawing": d, "alert": a, "level": level,
                                    "what": "crossed above", "price": price,
                                    "bar_time": None if intrabar else "2026-01-01 10:05", "intrabar": intrabar,
                                    "out": msg})
    return out


# ---- DrawingAlerts runs ----

def _alert(drawing_id: str, condition: str, mode: str = "once", armed_bar: int | None = None, **kw) -> dict:
    return {"drawing_id": drawing_id, "source": "synthetic", "symbol": "XAUUSD", "condition": condition, "mode": mode,
            "note": kw.get("note"), "expires_at": kw.get("expires_at"), "intrabar": kw.get("intrabar", False),
            "armed": kw.get("armed", True), "armed_bar": armed_bar, "fired_at": None, "fired_price": None,
            "fires": kw.get("fires", 0), "created_at": "2026-01-01T00:00:00+00:00"}


def _drawing(drawing_id: str, kind: str, points: list[dict], symbol: str = "XAUUSD") -> dict:
    return {"id": drawing_id, "source": "synthetic", "symbol": symbol, "kind": kind, "points": points,
            "style": {"color": None, "width": 1, "dash": "solid"}, "props": None, "timeframes": None,
            "locked": False, "hidden": False, "updated_at": "2026-01-01T00:00:00+00:00"}


def run_setup() -> dict:
    ids = [f"drawing-{i:04d}" for i in range(9)]
    drawings = [
        _drawing(ids[0], "hline", [_pt(T0, 2000.0)]),
        _drawing(ids[1], "trendline", [_pt(T0, 2000.0), _pt(T0 + 600, 2010.0)]),
        _drawing(ids[2], "rect", [_pt(T0 + 120, 2005.0), _pt(T0 + 900, 1995.0)]),
        _drawing(ids[3], "rect", [_pt(T0, 2005.0), _pt(T0 + 900, 1995.0)]),
        _drawing(ids[4], "hline", [_pt(T0, 2003.0)]),
        _drawing(ids[5], "hline", [_pt(T0, 1996.0)]),
        _drawing(ids[6], "path", [_pt(T0, 2000.0), _pt(T0 + 60, 2001.0)]),
        _drawing(ids[7], "hline", [_pt(T0, 2000.0)], symbol="OTHER"),
        _drawing(ids[8], "hline", [_pt(T0, 2002.0)]),
    ]
    alerts = [
        _alert(ids[0], "cross", "every", T0 - 120, note="Asia <high>", intrabar=True),
        _alert(ids[1], "cross_up", "once", T0 + 60),
        _alert(ids[2], "enter", "every", None),
        _alert(ids[3], "exit", "once", T0, intrabar=True, fires=2),
        _alert(ids[4], "cross_down", "every", None, expires_at="2001-01-01T00:00:00+00:00"),
        _alert(ids[5], "cross_down", "once", T0 + 60000),  # armed after the last bar: never
        _alert(ids[6], "cross", "every", None),  # not an alertable kind
        {**_alert(ids[7], "cross", "every", None), "symbol": "OTHER"},
        _alert("drawing-gone", "cross", "every", None),  # its drawing was deleted
        _alert(ids[8], "cross", "every", None, armed=False),
    ]
    m1 = walk_frame()
    n = len(m1)
    # Each step: check the closed bars up to index `upto`, a tick, a failing send, or a cache reset.
    steps = [
        {"check": 4},
        {"check": 4},
        {"tick": 2001.0, "at": T0 + 200.5},
        {"tick": 1999.0, "at": T0 + 230},
        {"tick": 1999.0, "at": T0 + 240},
        {"tick": 2004.0, "at": T0 + 250},
        {"check": 7, "fail": "Telegram said: chat not found"},
        {"tick": 2007.0, "at": T0 + 410},
        {"tick": 1994.0, "at": T0 + 420},
        {"check": 9, "send": False},
        {"invalidate": True},
        {"check": n},
        {"tick": float("nan"), "at": T0 + 900},
        {"tick": 2000.5, "at": T0 + 910},
        {"check": n},
    ]
    return {"drawings": drawings, "alerts": alerts, "steps": steps,
            "bars": [[int(t.timestamp()), float(r.open), float(r.high), float(r.low), float(r.close)]
                     for t, r in m1.iterrows()]}


def _times(row: dict, keys: tuple[str, ...]) -> dict:
    """Times of the moment the test ran, as a marker."""
    return {k: ("<now>" if k in keys and v is not None else v) for k, v in row.items()}


def run_case(tmp: Path) -> dict:
    setup = run_setup()
    store = Store(f"sqlite:///{tmp / 'run.db'}")
    for d in setup["drawings"]:
        store.drawing_put(d)
    for a in setup["alerts"]:
        store.drawing_alert_put(a)
    bars = pd.DataFrame(setup["bars"], columns=["t", "open", "high", "low", "close"])
    bars.index = pd.to_datetime(bars.pop("t"), unit="s")
    bars.index.name = None
    checker = DrawingAlerts(store)
    results = []
    for step in setup["steps"]:
        sent: list[str] = []

        def send(text: str, fail=step.get("fail")) -> None:
            if fail:
                raise RuntimeError(fail)
            sent.append(text)

        use = send if step.get("send", True) else None
        if "invalidate" in step:
            checker.invalidate()
            keys = []
        elif "check" in step:
            keys = checker.check("synthetic", "XAUUSD", bars.iloc[:step["check"]], use)
        else:
            keys = checker.check_tick("synthetic", "XAUUSD", step["tick"], pd.Timestamp(step["at"], unit="s"), use)
        results.append({"keys": keys, "sent": sent, "last_error": checker.last_error})
    alerts = sorted(store.drawing_alerts("synthetic", "XAUUSD") + store.drawing_alerts("synthetic", "OTHER"),
                    key=lambda a: a["drawing_id"])
    log = sorted(store.recent_alerts(1000), key=lambda r: r["key"])
    store.engine.dispose()
    setup["steps"] = [{**s, "tick": None} if isinstance(s.get("tick"), float) and s["tick"] != s["tick"] else s
                      for s in setup["steps"]]  # JSON has no NaN: null stands for it
    return {**setup, "results": results, "final_alerts": [_times(a, ("fired_at",)) for a in alerts],
            "log": [_times(r, ("sent_at",)) for r in log]}


def expected(tmp: Path) -> dict:
    return {"clean_drawing": drawing_cases(), "clean_alert": alert_cases(), "bar_events": bar_cases(),
            "tick_event": tick_cases(), "format_message": message_cases(), "run": run_case(tmp)}


def _dump(out: dict) -> str:
    """One case per line, so a diff shows which one moved."""
    parts = []
    for k, v in out.items():
        if isinstance(v, list):
            parts.append(json.dumps(k) + ": [\n" + ",\n".join(json.dumps(x, ensure_ascii=False) for x in v) + "\n]")
        else:
            parts.append(json.dumps(k) + ": " + json.dumps(v, ensure_ascii=False))
    return "{\n" + ",\n".join(parts) + "\n}\n"


def test_drawings_match_their_reference_output(tmp_path):
    now = json.loads(_dump(expected(tmp_path)))
    if os.environ.get("UPDATE_GOLDEN") or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(_dump(now), encoding="utf-8")
        if not os.environ.get("UPDATE_GOLDEN"):
            pytest.fail(f"wrote {GOLDEN}: review and commit it")
        return
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == now, (
        "The drawings' reference output changed. If that's meant, run\n"
        "  UPDATE_GOLDEN=1 uv run pytest tests/test_drawings_golden.py\nand review the diff.")


def test_the_run_fires_what_it_should():
    """The run exercises the paths worth porting: each kind fires, a failed send, a disarmed once."""
    if not GOLDEN.exists():
        pytest.skip("reference output not written yet")
    run = json.loads(GOLDEN.read_text(encoding="utf-8"))["run"]
    keys = [k for r in run["results"] for k in r["keys"]]
    assert any("|tick" in k for k in keys) and any("|bar" in k for k in keys)
    assert {k.split("|")[3] for k in keys} >= {"drawing-0000", "drawing-0001", "drawing-0002", "drawing-0003"}
    assert any(r["last_error"] for r in run["results"])
    statuses = {r["status"] for r in run["log"]}
    assert statuses == {"sent", "failed", "logged"}
    final = {a["drawing_id"]: a for a in run["final_alerts"]}
    assert final["drawing-0004"]["armed"] is False and final["drawing-0004"]["fires"] == 0  # expired
    assert final["drawing-0001"]["armed"] is False and final["drawing-0001"]["fires"] == 1
