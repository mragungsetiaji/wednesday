"""Reference data for the economic calendar (#96, step 9f): the real ``news.py`` and ``news_stats.py``
functions on fixed input, for the Rust port (``wed-server/tests/it/calendar_golden.rs``) to match
exactly (key order, ints and floats, float bits).

``tests/golden/calendar/`` holds the input, written once and kept (``input.json``: a ForexFactory
week, odd events for ``store_rows``, settings for ``upcoming``, figures, a stored calendar and the
reaction queries; ``m1-utc.json.gz`` and ``m1-ny7.json.gz``: M1 bars around the releases on the UTC
and NY+7 clocks; ``import/*.csv``: calendars to import), and ``output.json``, the output (not
``expected.json``, which marks a scan case for ``tests/test_golden.py``):

    UPDATE_GOLDEN=1 uv run pytest tests/test_calendar_golden.py
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wednesday.news import Calendar, CalendarSettings, parse_events
from wednesday.news_stats import (CalendarImportError, NewsReactions, history_summary, number, parse_csv,
                                  reaction, store_rows, surprise)
from wednesday.quarters import utc_to_feed
from wednesday.storage import Store

GOLDEN = Path(__file__).with_name("golden") / "calendar"
NOW = "2026-03-20T12:00:00+00:00"

# ---- the input ----

FEED = [  # ForexFactory's weekly JSON, with the odd rows the export has had
    {"title": "CPI m/m", "country": "USD", "date": "2026-03-18T08:30:00-04:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.2%"},
    {"title": "  Core   CPI m/m ", "country": "usd", "date": "2026-03-18T08:30:00-04:00", "impact": "high",
     "forecast": "", "previous": "0.3%"},
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-03-18T10:30:00-04:00", "impact": "Medium",
     "forecast": 0, "previous": -1.5},
    {"title": "ECB President Speaks", "country": "EUR", "date": "2026-03-18T09:00:00-04:00", "impact": "HIGH"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-03-19", "impact": "Holiday"},
    {"title": "All day, with a time", "country": "USD", "date": "2026-03-19T00:00:00-04:00", "impact": "Holiday"},
    {"title": "FOMC Statement", "country": "USD", "date": "2026-03-18T14:00:00-04:00", "impact": "High",
     "forecast": "4.50%", "previous": "4.50%"},
    {"title": "BOJ Policy Rate", "country": "JPY", "date": "2026-03-19T03:00:00.250000+00:00", "impact": "High",
     "forecast": "<0.10%", "previous": True},
    {"title": "RBI Rate", "country": "INR", "date": "2026-03-19T10:00:00+05:30", "impact": "Medium"},
    {"title": "Unemployment Claims", "country": "USD", "date": "2026-03-20T08:30:00-04:00", "impact": "High",
     "forecast": "225K", "previous": "221K"},
    {"title": "Philly Fed", "country": "USD", "date": "2026-03-20T08:30:00-04:00", "impact": "Medium"},
    {"title": "Late Speech", "country": "USD", "date": "2026-03-20T11:31:00Z", "impact": "High"},
    {"title": None, "country": None, "date": "2026-03-20T15:00:00Z", "impact": None},
    {"country": "GBP", "date": "2026-03-20T16:00:00+00:00"},
    {"title": "No date", "country": "USD", "impact": "High"},
    {"title": "Null date", "country": "USD", "date": None, "impact": "High"},
    {"title": "Number date", "country": "USD", "date": 20260320, "impact": "High"},
    {"title": "Bad date", "country": "USD", "date": "2026-03-32T08:30:00Z", "impact": "High"},
    {"title": "Tentative", "country": "USD", "date": "Tentative", "impact": "High"},
    {"title": "Retail Sales m/m", "country": "USD", "date": "2026-03-17T08:30:00-04:00", "impact": "Medium",
     "forecast": [], "previous": {"a": 1}},
]

ODD_EVENTS = [  # for store_rows, besides the parsed week
    {"time": "2026-03-16T13:30:00+00:00", "currency": "usd", "title": "  A \t  B ", "impact": "",
     "forecast": 0.25, "previous": 0, "actual": False},
    {"time": "2026-03-16T08:30:00-05:00", "currency": "ABCDEFGHIJ", "title": "T" * 250, "impact": "High",
     "forecast": "x" * 45, "previous": "   ", "actual": " 1.2% "},
    {"time": "2026-03-16T13:30:00.999999+00:00", "currency": "EUR", "title": "Fraction", "actual": True},
    {"time": "2026-03-16T13:30:00Z", "currency": "", "title": "No currency"},
    {"time": "2026-03-16T13:30:00Z", "currency": "USD", "title": "   "},
    {"currency": "USD", "title": "No time"},
    {"time": "not a time", "currency": "USD", "title": "Bad time"},
    {"time": 5, "currency": "USD", "title": "Number time"},
]

UPCOMING = [  # (now, settings)
    (NOW, {}),
    (NOW, {"currencies": ["USD", "EUR"], "impacts": ["High", "Medium"]}),
    ("2026-03-18T12:59:00+00:00", {}),
    ("2026-03-18T13:00:00+00:00", {}),
    ("2026-03-18T13:00:00.000001+00:00", {}),
    ("2026-03-18T13:00:00+00:00", {"currencies": ["JPY", "INR", "NONE"], "impacts": ["High", "Medium", "Low", ""]}),
    ("2026-03-21T00:00:00+00:00", {"currencies": ["USD"], "impacts": ["High", "Holiday"]}),
]

NUMBERS = ["0.3%", "-12K", "1.2M", "3B", "0.5T", "<0.1%", ">2", "~ 1.5", "−0.2%", "+4.25", "1,5", "1,234K",
           "12 k", "7m", "%", "", "   ", "n/a", "3.", ".5", "- 3", "--3", "0.10%x", "1e3", "١٢", "250K ",
           "　 42 %"]
SURPRISES = [["0.4%", "0.3%"], ["150K", "180K"], ["0.3%", "0.3%"], [None, "0.3%"], ["0.3%", None], ["n/a", "1"],
             ["1.0K", "1000"], ["-0.0", "0"]]

CLOCKS = {"utc": "UTC", "ny7": "NY+7"}


def _ny(day: str, clock: str = "08:30") -> str:
    return pd.Timestamp(f"{day} {clock}", tz="America/New_York").tz_convert("UTC").isoformat()


def calendar_rows() -> list[dict]:
    """The stored calendar: (event dict, how its bars look)."""
    rows = []
    figures = [("0.4%", "0.3%"), ("0.2%", "0.3%"), ("0.3%", "0.3%"), (None, "0.3%"), ("-0.1%", "0.1%"),
               ("1.2K", "1,1K"), ("0.3%", None)]
    months = pd.date_range("2024-01-01", periods=26, freq="MS")
    for i, m in enumerate(months):
        day = (m + pd.Timedelta(days=11)).strftime("%Y-%m-%d")
        actual, forecast = figures[i % len(figures)]
        defect = {3: "after40", 7: "gap_before", 11: "short_history", 20: "late_start"}.get(i, "")
        rows.append(({"time": _ny(day), "currency": "USD", "title": "CPI m/m", "impact": "High",
                      "actual": actual, "forecast": forecast, "previous": "0.2%"}, defect))
        if i % 3 == 0:
            rows.append(({"time": _ny(day), "currency": "USD", "title": "Core CPI m/m", "impact": "High",
                          "actual": forecast, "forecast": actual}, defect))
    rows.append(({"time": _ny("2026-03-11"), "currency": "usd", "title": "cpi   M/M", "impact": "High",
                  "actual": "0.5%", "forecast": "0.3%"}, ""))
    for i, (day, defect) in enumerate([("2025-06-06", ""), ("2025-07-03", "late_start"), ("2025-08-01", "early_end"),
                                       ("2025-09-05", "flat"), ("2025-10-03", "none"), ("2025-11-07", ""),
                                       ("2025-12-05", "after49"), ("2026-01-09", "")]):
        rows.append(({"time": _ny(day), "currency": "USD", "title": "Non-Farm Employment Change", "impact": "High",
                      "actual": f"{150 + 20 * i}K", "forecast": "180K"}, defect))
    for day in ("2025-10-30", "2025-12-18", "2026-01-29", "2026-03-12"):
        rows.append(({"time": f"{day}T13:45:00+00:00", "currency": "EUR", "title": "ECB Press Conference",
                      "impact": "High"}, ""))
    for day in ("2025-11-14", "2025-12-16", "2026-01-15", "2026-02-13"):
        rows.append(({"time": _ny(day), "currency": "USD", "title": "Retail Sales m/m", "impact": "Medium",
                      "actual": "0.6%", "forecast": "0.4%"}, ""))
    for day in ("2025-10-29", "2025-12-10", "2026-01-28"):
        rows.append(({"time": _ny(day, "14:00"), "currency": "USD", "title": "FOMC Statement", "impact": None}, ""))
    rows.append(({"time": "2026-03-20T11:30:00+00:00", "currency": "USD", "title": "Unemployment Claims",
                  "impact": "High"}, ""))
    rows.append(({"time": "2026-03-20T10:00:00+00:00", "currency": "USD", "title": "Unemployment Claims",
                  "impact": "High", "actual": "230K", "forecast": "225K"}, ""))
    rows.append(({"time": "2026-03-20T10:58:59+00:00", "currency": "USD", "title": "Just over an hour",
                  "impact": "High"}, ""))
    rows.append(({"time": "2025-12-25T05:00:00+00:00", "currency": "USD", "title": "Christmas Day",
                  "impact": "Holiday"}, "none"))
    return rows


REACTION_CASES = [
    {"clock": "UTC", "bars": "utc", "currencies": ["USD"], "impacts": ["High"]},
    {"clock": "NY+7", "bars": "ny7", "currencies": ["USD"], "impacts": ["High"]},
    {"clock": "UTC", "bars": "ny7", "currencies": ["USD"], "impacts": ["High"]},
    {"clock": "UTC", "bars": "utc", "currencies": ["USD", "EUR"], "impacts": ["High", "Medium"]},
    {"clock": "NY+7", "bars": "ny7", "currencies": ["EUR"], "impacts": []},
    {"clock": "UTC", "bars": "utc", "currencies": [], "impacts": []},
]


def make_bars(clock: str) -> pd.DataFrame:
    """M1 bars around each release, on ``clock``: a random walk with a move after the release, and
    the release's defect (bars missing after it, a gap before it, a flat market, ...)."""
    frames, seen = [], set()
    for i, (e, defect) in enumerate(calendar_rows()):
        utc = pd.Timestamp(e["time"])
        if utc in seen:
            continue
        seen.add(utc)
        rng = np.random.default_rng(1000 + i)
        t0 = utc_to_feed(utc, clock).floor("min")
        idx = pd.date_range(t0 - pd.Timedelta(minutes=240), t0 + pd.Timedelta(minutes=89), freq="1min")
        mins = np.asarray((idx - t0) / pd.Timedelta(minutes=1)).astype(int)
        steps = rng.normal(0, 0.6, len(idx)) + np.where(mins == 0, rng.normal(0, 6), 0)
        close = np.round(2000 + np.cumsum(steps), 2)
        if defect == "flat":
            close[:] = 2000.0
        open_ = np.round(np.r_[close[0], close[:-1]], 2)
        wick = 0 if defect == "flat" else 1
        high = np.round(np.maximum(open_, close) + wick * np.abs(rng.normal(0, 0.3, len(idx))), 2)
        low = np.round(np.minimum(open_, close) - wick * np.abs(rng.normal(0, 0.3, len(idx))), 2)
        df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)
        keep = rng.random(len(idx)) > 0.04  # a few missing minutes
        keep[(mins >= -1) & (mins <= 1)] = True
        if defect == "after40":
            keep &= ~((mins >= 40) & (mins < 60))
        elif defect == "after49":
            keep &= ~((mins >= 49) & (mins < 60))
            keep[(mins >= 0) & (mins < 49)] = True
        elif defect == "gap_before":
            keep &= ~((mins >= -10) & (mins < 0))
        elif defect == "late_start":
            keep &= ~((mins >= 0) & (mins < 3))
        elif defect == "early_end":
            keep &= ~(mins >= 50)
        elif defect == "short_history":
            keep &= mins >= -60
        elif defect == "none":
            keep[:] = False
        frames.append(df[keep])
    return pd.concat(frames).sort_index()


def write_bars(path: Path, df: pd.DataFrame) -> None:
    rows = [[int(t.timestamp()), *map(float, r)] for t, r in zip(df.index, df.to_numpy())]
    with gzip.GzipFile(path, "wb", mtime=0) as fh:
        fh.write(json.dumps(rows).encode())


def read_bars(path: Path) -> pd.DataFrame:
    rows = json.loads(gzip.decompress(path.read_bytes()))
    df = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close", "volume"],
                      index=pd.to_datetime([r[0] for r in rows], unit="s"))
    return df


IMPORTS = {  # file -> content (bytes)
    "ff-export.csv": (b"Title,Country,Date,Time,Impact,Actual,Forecast,Previous\n"
                      b"Non-Farm Employment Change,USD,01-09-2026,8:30am,High,256K,164K,212K\n"
                      b"Bank Holiday,USD,01-19-2026,All Day,Holiday,,,\n"
                      b"\"Fed Chair Powell Speaks, Again\",usd,01-20-2026,2:00pm,high,,,\n"
                      b"Tentative Thing,USD,01-21-2026,Tentative,Medium,,,\n"
                      b"Midnight,EUR,03-08-2026,12:00am,Low,,,\n"
                      b"After the change,USD,03-09-2026,8:30am,High,0.4%,0.3%,0.2%\n"
                      b"Autumn,USD,11-02-2026,1:30am,High,,,\n"),
    "iso.csv": ("﻿datetime,currency,event,impact,actual,forecast,previous\r\n"
                "2026-01-09T08:30:00-05:00,usd,NFP,HIGH,256K,164K,212K\r\n"
                "\r\n"
                "2026-01-09T13:30:00Z,USD,NFP again,,,,\r\n"
                "2026-01-10 08:30,EUR,Naive in UTC,Medium,  1.5% , 1.4% ,\r\n"
                "2026-01-10 08:30:15.75,EUR,Fraction,Low,,,\r\n").encode(),
    "formats.csv": (b"datetime,currency,title\n"
                    b"01/09/2026 8:30am,USD,slashes\n"
                    b"1/9/26 8:30 PM,USD,short year\n"
                    b"13/01/2026 08:30,USD,day first\n"
                    b"\"Jan 9, 2026 8:30 am\",USD,month name\n"
                    b"9 January 2026 08:30,USD,long month\n"
                    b"\"Friday, January 9, 2026 8:30am\",USD,weekday\n"
                    b"2026.01.09 08:30,USD,dots\n"
                    b"20260109 08:30,USD,compact\n"
                    b"2026-01-09T08:30:00+0530,USD,offset\n"
                    b"2026-01-09 08:30 GMT,USD,gmt\n"
                    b"Jan 9th 2026 8am,USD,ordinal\n"
                    b"2026-01-09 12 pm,USD,noon\n"),
    "latin1.csv": "Title,Country,Date,Time\nRéunion,EUR,2026-01-09,08:30\n".encode("latin-1"),
    "messy-header.csv": (b" TITLE , Country ,Date,date,Time,Extra\n"
                         b"Dup,USD,2026-01-01,2026-01-09,08:30,x,y,z\n"
                         b"Short,USD,2026-01-01\n"
                         b"Quoted,\"USD\",\"2026-01-0\"\"9\",2026-01-09,\"09:\n30\"\n"),
    "no-header.csv": b"a,b\n1,2\n",
    "empty.csv": b"",
    "blank-first.csv": b"\ncurrency,title,datetime\nUSD,X,2026-01-09 08:30\n",
    "bad-time.csv": b"datetime,currency,title\n2026-01-09 08:30,USD,ok\n2026-13-45 99:99,USD,X\n",
    "no-times.csv": b"date,time,currency,title\n2026-01-09,All Day,USD,Holiday\n2026-01-10,,USD,Nothing\n",
    "no-currency.csv": b"datetime,currency,title\n2026-01-09 08:30,,X\n",
    "skipped-hour.csv": b"datetime,currency,title\n2026-03-08 02:30,USD,Spring\n",
    "repeated-hour.csv": b"datetime,currency,title\n2026-11-01 01:30,USD,Autumn\n",
    "bare-cr.csv": b"datetime,currency,title\r2026-01-09 08:30,USD,X\r",
    "unclosed-quote.csv": b"datetime,currency,title\n2026-01-09 08:30,USD,\"open title",
}

IMPORT_CASES = [
    ("ff-export.csv", "America/New_York"), ("ff-export.csv", "UTC"), ("iso.csv", "UTC"),
    ("iso.csv", "America/New_York"), ("formats.csv", "UTC"), ("latin1.csv", "UTC"),
    ("messy-header.csv", "UTC"), ("no-header.csv", "UTC"), ("empty.csv", "UTC"), ("blank-first.csv", "UTC"),
    ("bad-time.csv", "UTC"), ("no-times.csv", "UTC"), ("no-currency.csv", "UTC"),
    ("skipped-hour.csv", "America/New_York"), ("skipped-hour.csv", "UTC"), ("repeated-hour.csv", "America/New_York"),
    ("bare-cr.csv", "UTC"), ("unclosed-quote.csv", "UTC"), ("iso.csv", "Asia/Jakarta"), ("iso.csv", "utc"),
]

STAMPS = [  # one per file: datetime,currency,title
    "2026-01-09 08:30", "2026-01-09T13:30:00Z", "2026-01-09 08:30:00-05:00", "01/09/2026 8:30 am",
    "1/9/26 8:30 pm", "Jan 9, 2026 8:30 am", "9 Jan 2026 08:30", "2026.01.09 08:30", "Fri Jan 9 2026 8:30 am",
    "13/01/2026 08:30", "2026-1-9 8:30", "January 9 2026 08:30", "2026/01/09 08:30:15.5", "09.01.2026 08:30",
    "2026-01-09 08:30 UTC", "Tue 2026-01-09 08:30", "2026-01-09T08:30:00+0530", "2026-01-09 12 am",
    "2026-01-09 12:30 am", "5/1/2026 8:30", "Jan 09 2026 08:30AM", "2026-01-09 8:30am", "24/01/05 8:30",
    "1/5/24 8am", "Jan 5th 2024 8:30", "5 January, 2024 at 8:30", "2024-01-05 8:30 PM", "Sept 5 2024 08:30",
    "2024-01-05 08:30:00 +05:30", "2024-01-05 08:30+05", "2024-01-05 08:30 Z", "Jan-05-2024 08:30",
    "05-Jan-2024 08:30", "2024-Jan-05 08:30", "2024-01-05 8:30 a.m.", "2024-01-05, 08:30",
    "Friday, January 5, 2024 8:30am", "2024-01-05 08:30:15.123456789", "2024-01-05 0:00",
    "01/05/2024 12:00am", "01/05/2024 12:30pm", "2024-01-05T08:30", "1999-12-31 23:59", "12/31/99 11:59 pm",
    # refused
    "2024-01-05 13:00 pm", "2024-01-05 08:30 utc", "2024-01-05 08:30 EST", "2024-02-30 08:30",
    "2024-01-05 08:30:60", "2024-01-05 25:30", "2024-13-05 08:30", "08:30 on the fifth", "8:30amx",
    # no clock time: skipped
    "2024-01-05", "All Day", "2024-01-05 08.30",
]


def stamp_file(stamp: str) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["datetime", "currency", "title"])
    w.writerow([stamp, "USD", "X"])
    return buf.getvalue().encode()


def write_input() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    (GOLDEN / "import").mkdir(exist_ok=True)
    for name, data in IMPORTS.items():
        (GOLDEN / "import" / name).write_bytes(data)
    for key, clock in CLOCKS.items():
        write_bars(GOLDEN / f"m1-{key}.json.gz", make_bars(clock))
    inp = {
        "now": NOW,
        "feed": FEED,
        "odd_events": ODD_EVENTS,
        "upcoming": [{"now": n, "settings": s} for n, s in UPCOMING],
        "numbers": NUMBERS,
        "surprises": SURPRISES,
        "calendar": [e for e, _ in calendar_rows()],
        "reactions": REACTION_CASES,
        "imports": [{"file": f, "zone": z} for f, z in IMPORT_CASES],
        "stamps": STAMPS,
    }
    (GOLDEN / "input.json").write_text(json.dumps(inp, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ---- the output ----

def _import(data: bytes, zone: str) -> dict:
    try:
        return {"rows": parse_csv(data, zone)}
    except CalendarImportError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the route doesn't catch these: a 500
        return {"crash": type(exc).__name__}


def expected_output(tmp: Path) -> dict:
    inp = json.loads((GOLDEN / "input.json").read_text(encoding="utf-8"))
    now = datetime.fromisoformat(inp["now"])
    events = parse_events(inp["feed"])

    upcoming = []
    for case in inp["upcoming"]:
        cal = Calendar(None, CalendarSettings.from_dict(case["settings"]))
        cal.events = events
        upcoming.append({"upcoming": cal.upcoming(datetime.fromisoformat(case["now"])), "matching": cal.matching()})

    store = Store(f"sqlite:///{tmp / 'golden.db'}")
    rows = store_rows(inp["calendar"], source="csv")
    store.calendar_put(rows)
    bars = {key: read_bars(GOLDEN / f"m1-{key}.json.gz") for key in CLOCKS}
    singles = []
    for key, clock in CLOCKS.items():
        m1 = bars[key]
        for r in rows:
            t0 = utc_to_feed(pd.Timestamp(r["time"], unit="s", tz="UTC"), clock)
            got = reaction(m1[(m1.index >= t0 - pd.Timedelta(minutes=240)) & (m1.index <= t0 + pd.Timedelta(minutes=61))], t0)
            singles.append({"clock": clock, "time": r["time"], "title": r["title"], "reaction": got})
    stats = []
    for case in inp["reactions"]:
        m1 = bars[case["bars"]]

        def window(start, end, m1=m1):
            return m1[(m1.index >= start) & (m1.index <= end)]

        out = NewsReactions(store).stats(window, case["clock"], case["currencies"], case["impacts"],
                                         key=("golden", case["bars"]), now=now.timestamp())
        stats.append(out)

    stamps = []
    for s in inp["stamps"]:
        got = _import(stamp_file(s), "UTC")
        stamps.append({"stamp": s, **({"time": got["rows"][0]["time"]} if "rows" in got else got)})

    return {
        "parse": events,
        "store_rows": store_rows(events) + store_rows(inp["odd_events"], source="csv"),
        "upcoming": upcoming,
        "numbers": [number(x) for x in inp["numbers"]],
        "surprises": [surprise(a, f) for a, f in inp["surprises"]],
        "history": store.calendar_history(),
        "history_summary": history_summary(store),
        "reactions": singles,
        "stats": stats,
        "imports": [_import((GOLDEN / "import" / c["file"]).read_bytes(), c["zone"]) for c in inp["imports"]],
        "stamps": stamps,
    }


def _dump(out: dict) -> str:
    """One top-level key per line block, list items one per line, so a diff shows what moved."""
    parts = []
    for k, v in out.items():
        if isinstance(v, list):
            body = "[\n" + ",\n".join(json.dumps(x, ensure_ascii=False) for x in v) + "\n]"
        else:
            body = json.dumps(v, ensure_ascii=False)
        parts.append(f"{json.dumps(k)}: {body}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def test_the_calendar_matches_its_reference_output(tmp_path):
    if not (GOLDEN / "input.json").exists():
        write_input()
    now = json.loads(_dump(expected_output(tmp_path)))
    path = GOLDEN / "output.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        path.write_text(_dump(now), encoding="utf-8")
        if not os.environ.get("UPDATE_GOLDEN"):
            pytest.fail(f"wrote {path.relative_to(GOLDEN.parent.parent)}: review and commit it")
        return
    assert json.loads(path.read_text(encoding="utf-8")) == now, (
        "The calendar's output changed. If that's meant, run\n"
        "  UPDATE_GOLDEN=1 uv run pytest tests/test_calendar_golden.py\nand review the diff.")


def test_the_cases_cover_what_they_say():
    """The reference output exercises the paths worth porting."""
    path = GOLDEN / "output.json"
    if not path.exists():
        pytest.skip("reference output not written yet")
    out = json.loads(path.read_text(encoding="utf-8"))
    singles = [s["reaction"] for s in out["reactions"]]
    assert any(r is None for r in singles) and any(r and r["atr"] is None for r in singles)
    assert any(r and r["reversed"] for r in singles) and any(r and not r["reversed"] for r in singles)
    types = [t for s in out["stats"] for t in s.values()]
    assert any(t["count"] == 24 and t["stored"] > 24 for t in types)  # the limit
    assert any(t["count"] == 0 for t in types) and any(t["surprise"]["above"]["count"] for t in types)
    assert {"rows", "error", "crash"} <= {k for i in out["imports"] for k in i}
    assert {"time", "error"} <= {k for s in out["stamps"] for k in s}
    assert any("rows" in s or "error" in s and "No row" in s["error"] for s in out["stamps"])
