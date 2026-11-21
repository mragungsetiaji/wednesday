"""Reference data for the move distribution (#96): M1 bars and the feed's daily bars in, the period
tables and ``distribution()``'s answer out, for many periods, measures, lookbacks and filters.

Each case in ``tests/golden/distribution/<case>/`` has the input, ``m1.csv.gz`` (closed M1 bars on
the feed's clock) and ``d1.csv.gz`` (daily bars by New York trading day, as ``daily_history`` gives
them; absent when the case has none), and ``expected.json``: the tables (``tables(quarter_blocks(m1),
d1)``) and the answer to each query. The Rust port (``wed-server/tests/it/distribution_golden.rs``) must
give the same output, floats to the last bit.

    UPDATE_GOLDEN=1 uv run pytest tests/test_distribution_golden.py

The input is generated once from a seeded random walk, rounded to the case's tick, and only
rewritten when a case's file is missing.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wednesday.distribution import MEASURES, PERIODS, distribution, tables
from wednesday.feeds import SyntheticFeed
from wednesday.quarters import quarter_blocks, to_new_york, utc_index_to_feed

GOLDEN = Path(__file__).with_name("golden") / "distribution"
LOOKBACKS = ("1y", "5y", "all")


@dataclass
class Case:
    name: str
    seed: int
    clock: str = "UTC"
    end: str = "2026-03-04 15:00"  # UTC
    minutes: int = 30_000  # M1 history before the closed hours are dropped
    tick: float = 0.01
    daily: int = 0  # daily bars from the feed (0: none)
    note: str = ""


CASES = [
    Case("utc-daily", seed=11, daily=1800,
         note="M1 for a few weeks, the feed's daily bars five years back (overlapping the M1 days, a flat run "
              "for the ATR, weekend rows dropped)"),
    Case("ny7-friday", seed=23, clock="NY+7", end="2026-03-06 21:30", minutes=12_000,
         note="a broker clock, ending Friday before the close: no daily bars, the week and day still forming"),
    Case("ties", seed=4, tick=0.5, minutes=9_000, daily=60,
         note="prices on a 0.50 grid: equal moves everywhere, so the order of ties is exercised"),
    Case("weekend", seed=8, end="2026-03-08 12:00", minutes=600,
         note="only weekend minutes: no periods at all"),
    Case("weekend-daily", seed=9, end="2026-03-08 12:00", minutes=600, daily=120,
         note="only weekend minutes, so the days are the feed's daily bars alone: the last week closed on Friday"),
]


# ---- the input ----

def _market_hours(index: pd.DatetimeIndex) -> np.ndarray:
    ny = to_new_york(index, "UTC")
    day, hour = ny.dayofweek, ny.hour
    weekend = (day == 5) | ((day == 4) & (hour >= 17)) | ((day == 6) & (hour < 18))
    return ~(weekend | (hour == 17))


def make_m1(case: Case) -> pd.DataFrame:
    feed = SyntheticFeed(seed=case.seed, history=case.minutes, end=pd.Timestamp(case.end))
    bars = feed._bars
    if not case.name.startswith("weekend"):
        bars = bars[_market_hours(bars.index)]
    else:
        bars = bars[~_market_hours(bars.index)]
    prices = ["open", "high", "low", "close"]
    bars[prices] = ((bars[prices] / case.tick).round() * case.tick).round(2)
    bars["volume"] = bars["volume"].astype(int)
    bars.index = utc_index_to_feed(bars.index.tz_localize("UTC"), case.clock)
    return bars


def make_d1(case: Case, m1: pd.DataFrame) -> pd.DataFrame | None:
    if not case.daily:
        return None
    rng = np.random.default_rng(case.seed + 1000)
    last = (to_new_york(m1.index[-1:], case.clock) + pd.Timedelta(hours=7)).normalize()[0] - pd.Timedelta(days=1)
    days = pd.date_range(end=last, periods=case.daily, freq="D")
    days = days[(days.dayofweek < 5) | (np.arange(len(days)) % 17 == 0)]  # a few weekend rows, which are dropped
    close = 1300 * np.cumprod(1 + rng.normal(0.0002, 0.011, len(days)))
    opens = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.001, len(days)))
    high = np.maximum(opens, close) * (1 + np.abs(rng.normal(0, 0.004, len(days))))
    low = np.minimum(opens, close) * (1 - np.abs(rng.normal(0, 0.004, len(days))))
    df = pd.DataFrame({"open": opens, "high": high, "low": low, "close": close, "volume": 0.0}, index=days)
    if len(df) > 400:
        df.iloc[200:225, :4] = df.iloc[200, :4].to_numpy()  # a flat run: equal true ranges
        df.iloc[200:225, 1] = df.iloc[200, 0] + 5.0
        df.iloc[200:225, 2] = df.iloc[200, 0] - 5.0
        df.iloc[200:225, 3] = df.iloc[200, 0]
        df.iloc[200:225, 0] = df.iloc[200, 0]
    cols = ["open", "high", "low", "close"]
    df[cols] = ((df[cols] / case.tick).round() * case.tick).round(2)
    return df


def write_input(case: Case, m1: pd.DataFrame, d1: pd.DataFrame | None) -> None:
    folder = GOLDEN / case.name
    folder.mkdir(parents=True, exist_ok=True)
    out = m1.copy()
    out.index = out.index.strftime("%Y-%m-%dT%H:%M")
    out.index.name = "time"
    out.to_csv(folder / "m1.csv.gz", float_format="%.2f", compression={"method": "gzip", "mtime": 0})
    if d1 is not None:
        out = d1.copy()
        out.index = out.index.strftime("%Y-%m-%d")
        out.index.name = "time"
        out.to_csv(folder / "d1.csv.gz", float_format="%.2f", compression={"method": "gzip", "mtime": 0})


def read_input(case: Case) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    folder = GOLDEN / case.name
    m1 = pd.read_csv(folder / "m1.csv.gz", index_col="time", parse_dates=["time"]).astype(float)
    d1 = None
    if (folder / "d1.csv.gz").exists():
        d1 = pd.read_csv(folder / "d1.csv.gz", index_col="time", parse_dates=["time"]).astype(float)
    return m1, d1


# ---- the output ----

def queries() -> list[dict]:
    out = []
    for period in PERIODS:
        for measure in MEASURES:
            for lookback in LOOKBACKS:
                out.append({"period": period, "measure": measure, "lookback": lookback, "weekday": False,
                            "session": False, "bins": 40})
            for weekday, session in ((True, False), (False, True), (True, True)):
                out.append({"period": period, "measure": measure, "lookback": "all", "weekday": weekday,
                            "session": session, "bins": 40})
    for bins in (10, 13, 80):
        out.append({"period": "q90", "measure": "change", "lookback": "1y", "weekday": False, "session": False,
                    "bins": bins})
    for bad in ({"period": "month"}, {"lookback": "2y"}, {"measure": "size"}, {"period": "month", "measure": "size"}):
        out.append({"period": "day", "measure": "change", "lookback": "1y", "weekday": False, "session": False,
                    "bins": 40, **bad})
    return out


def _rows(df: pd.DataFrame) -> list[list]:
    return [[r.day.date().isoformat(), str(r.label), int(pd.Timestamp(r.start).timestamp()), float(r.open),
             float(r.high), float(r.low), float(r.close), bool(r.live)] for r in df.itertuples(index=False)]


def expected_output(case: Case, m1: pd.DataFrame, d1: pd.DataFrame | None) -> dict:
    t = tables(quarter_blocks(m1, case.clock), d1)
    answers = []
    for q in queries():
        try:
            answer = {"out": distribution(t, q["period"], q["measure"], q["lookback"], q["weekday"], q["session"],
                                          q["bins"])}
        except ValueError as exc:
            answer = {"error": str(exc)}
        answers.append({"query": q, **answer})
    return {"case": {"name": case.name, "clock": case.clock, "note": case.note},
            "tables": {name: _rows(df) for name, df in t.items()}, "answers": answers}


def differences(expected, actual, path="", limit=20) -> list[str]:
    out: list[str] = []

    def walk(e, a, p):
        if len(out) >= limit:
            return
        if isinstance(e, dict) and isinstance(a, dict):
            for k in sorted(e.keys() | a.keys()):
                if k not in a or k not in e:
                    out.append(f"{p}.{k}: {'missing' if k not in a else 'new'}")
                else:
                    walk(e[k], a[k], f"{p}.{k}")
        elif isinstance(e, list) and isinstance(a, list):
            if len(e) != len(a):
                out.append(f"{p}: {len(e)} items became {len(a)}")
            for i, (x, y) in enumerate(zip(e, a)):
                walk(x, y, f"{p}[{i}]")
        elif isinstance(e, float) and isinstance(a, float):
            if not math.isclose(e, a, rel_tol=1e-12, abs_tol=1e-12):
                out.append(f"{p}: {e} became {a}")
        elif type(e) is not type(a) or e != a:
            out.append(f"{p}: {json.dumps(e)[:60]} became {json.dumps(a)[:60]}")

    walk(expected, actual, path)
    return out


def _dump(out: dict) -> str:
    """One answer per line, so a diff shows which query moved."""
    head = json.dumps({k: v for k, v in out.items() if k != "answers"}, ensure_ascii=False)
    lines = ",\n".join(json.dumps(a, ensure_ascii=False) for a in out["answers"])
    return head[:-1] + ',\n"answers": [\n' + lines + "\n]}\n"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_distribution_matches_its_reference_output(case):
    if not (GOLDEN / case.name / "m1.csv.gz").exists():
        m1 = make_m1(case)
        write_input(case, m1, make_d1(case, m1))
    m1, d1 = read_input(case)
    now = expected_output(case, m1, d1)
    path = GOLDEN / case.name / "expected.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        path.write_text(_dump(now), encoding="utf-8")
        if not os.environ.get("UPDATE_GOLDEN"):
            pytest.fail(f"wrote {path.relative_to(GOLDEN.parent.parent)}: review and commit it")
        return
    problems = differences(json.loads(path.read_text(encoding="utf-8")), now)
    assert not problems, (
        f"The distribution on {case.name} changed. If that's meant, run\n"
        "  UPDATE_GOLDEN=1 uv run pytest tests/test_distribution_golden.py\n"
        "and review the diff:\n  " + "\n  ".join(problems))


def test_the_cases_cover_what_they_say():
    """The reference answers exercise the paths worth porting: percentiles, the like-this list with
    ties on one day, the ATR, the daily bars, and an empty table."""
    seen = {"percentile": 0, "ties": 0, "atr": 0, "empty": 0, "errors": 0}
    for case in CASES:
        path = GOLDEN / case.name / "expected.json"
        if not path.exists():
            pytest.skip("reference output not written yet")
        for a in json.loads(path.read_text(encoding="utf-8"))["answers"]:
            out = a.get("out")
            if out is None:
                seen["errors"] += 1
                continue
            seen["percentile"] += out["percentile"] is not None
            days = [r["day"] for r in out["like_this"]]
            seen["ties"] += len(days) != len(set(days))
            seen["atr"] += out["measure"] == "range_atr" and out["samples"] > 0
            seen["empty"] += out["current"] is None
    assert all(seen.values()), seen
