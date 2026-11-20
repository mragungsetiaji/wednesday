"""Reference data for the scan (#96): M1 candles in, what Wednesday makes of them out.

Each case in ``tests/golden/<case>/`` has the input, ``m1.csv.gz`` (closed M1 bars on the feed's
clock), and ``expected.json``: the closed candles each timeframe scans, the swings and the latest
break of structure, every level each detector finds (active or ended), the scan with its setups
(with no bias, bullish and bearish), the session reference levels and the quarterly blocks.

Any other implementation of the scan (a Rust one, say) must give the same output on the same
input files. Rewrite the expected output after a deliberate change to the detectors, and review
the diff: a level that moved is a level the trader sees move.

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py

Times in the output are feed-clock ISO strings and unix seconds (the feed time read as UTC, as the
chart does). The input is generated once from a seeded random walk, rounded to cents, and only
rewritten when a case's file is missing.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wednesday.bias import TradeBias
from wednesday.detectors import DetectorParams, build_detectors
from wednesday.feeds import SyntheticFeed
from wednesday.quarters import quarters_payload, to_new_york, utc_index_to_feed
from wednesday.scanner import ScanConfig, scan
from wednesday.sessions import reference_levels
from wednesday.structure import Context, label_swings, latest_bias
from wednesday.timeframes import resample_ohlcv

GOLDEN = Path(__file__).with_name("golden")


@dataclass
class Case:
    name: str
    seed: int
    clock: str = "UTC"
    end: str = "2026-03-04 15:00"  # UTC, a Wednesday in New York's afternoon
    lookback: int = 100
    params: dict = field(default_factory=dict)  # DetectorParams overrides
    tick: float = 0.01  # prices are rounded to this; a coarse one makes equal highs and lows common
    note: str = ""


CASES = [
    Case("utc-default", seed=7, note="Yahoo / TradingView clock, default detector settings"),
    Case("utc-wick-close", seed=21, params={"zone": "wick", "mitigation": "close", "swing_length": 3},
         note="full-candle zones, mitigation on a close beyond, shorter swings"),
    Case("broker-ny7", seed=42, clock="NY+7", end="2026-03-06 18:00",
         note="a broker clock (New York + 7h, as many MT5 servers), ending Friday near the close"),
    Case("utc-ties", seed=5, tick=0.5,
         note="prices on a 0.50 grid: equal highs and lows everywhere, so every tie-break is exercised"),
]


# ---- the input ----

def _market_hours(index: pd.DatetimeIndex, clock: str = "UTC") -> np.ndarray:
    """True for minutes gold trades: not the weekend (Fri 17:00 to Sun 18:00 New York), not the
    daily break (17:00 to 18:00 New York)."""
    ny = to_new_york(index, clock)
    day, hour = ny.dayofweek, ny.hour
    weekend = (day == 5) | ((day == 4) & (hour >= 17)) | ((day == 6) & (hour < 18))
    return ~(weekend | (hour == 17))


def make_input(case: Case) -> pd.DataFrame:
    cfg = ScanConfig(lookback=case.lookback)
    # Enough minutes that, after the closed hours, every timeframe still has its lookback.
    feed = SyntheticFeed(seed=case.seed, history=int(cfg.required_m1_bars() * 1.6), end=pd.Timestamp(case.end))
    bars = feed._bars
    bars = bars[_market_hours(bars.index)]
    prices = ["open", "high", "low", "close"]
    bars[prices] = ((bars[prices] / case.tick).round() * case.tick).round(2)  # monotonic: high stays the highest
    bars["volume"] = bars["volume"].astype(int)
    bars.index = utc_index_to_feed(bars.index.tz_localize("UTC"), case.clock)
    return bars


def write_input(case: Case, bars: pd.DataFrame) -> None:
    out = bars.copy()
    out.index = out.index.strftime("%Y-%m-%dT%H:%M")
    out.index.name = "time"
    (GOLDEN / case.name).mkdir(parents=True, exist_ok=True)
    out.to_csv(GOLDEN / case.name / "m1.csv.gz", float_format="%.2f", compression={"method": "gzip", "mtime": 0})


def read_input(case: Case) -> pd.DataFrame:
    df = pd.read_csv(GOLDEN / case.name / "m1.csv.gz", index_col="time", parse_dates=["time"])
    return df.astype(float)


# ---- the output ----

def _clean(value):
    """JSON-safe: numpy numbers become plain ones, timestamps ISO strings."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        v = float(value)
        return None if math.isnan(v) else v
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def expected_output(case: Case, m1: pd.DataFrame) -> dict:
    params = DetectorParams(**case.params)
    cfg = ScanConfig(lookback=case.lookback, params=params)
    price = float(m1["close"].iloc[-1])
    out: dict = {
        "case": {**asdict(case), "params": asdict(params), "price": price, "m1_bars": len(m1),
                 "detectors": list(cfg.detectors), "recent_bars": cfg.recent_bars},
        "timeframes": {},
    }
    for tf in sorted(cfg.timeframes, key=lambda t: t.minutes, reverse=True):
        candles = resample_ohlcv(m1, tf).tail(cfg.lookback)
        ctx = Context(candles)
        bias = latest_bias(ctx, params.swing_length)
        out["timeframes"][tf.name] = {
            # [open time (unix), open, high, low, close, volume], closed candles only
            "candles": [[int(t.timestamp()), *row] for t, row in
                        zip(candles.index, candles[["open", "high", "low", "close", "volume"]].to_numpy().tolist())],
            "swings": [s.to_dict() for s in label_swings(ctx.structure(params.swing_length), ctx.times)],
            "bias": bias.to_dict() if bias else None,
            "levels": {d.name: [lv.to_dict() for lv in d.detect(ctx)] for d in build_detectors(cfg.detectors, params)},
        }
    result = scan(m1, cfg, price)
    out["scan"] = result.to_dict()
    out["setups"] = {
        direction: result.to_dict(TradeBias(direction))["setups"] for direction in ("bullish", "bearish", "neutral")
    }
    out["sessions"] = reference_levels(m1, case.clock)
    out["quarters"] = quarters_payload(m1, case.clock)
    return _clean(out)


def differences(expected, actual, path="", limit=20) -> list[str]:
    out: list[str] = []

    def walk(e, a, p):
        if len(out) >= limit:
            return
        if isinstance(e, dict) and isinstance(a, dict):
            for k in sorted(e.keys() | a.keys()):
                if k not in a:
                    out.append(f"{p}.{k}: missing")
                elif k not in e:
                    out.append(f"{p}.{k}: new")
                else:
                    walk(e[k], a[k], f"{p}.{k}")
        elif isinstance(e, list) and isinstance(a, list):
            if len(e) != len(a):
                out.append(f"{p}: {len(e)} items became {len(a)}")
            for i, (x, y) in enumerate(zip(e, a)):
                walk(x, y, f"{p}[{i}]")
        elif isinstance(e, float) and isinstance(a, (int, float)) and not isinstance(a, bool):
            if not math.isclose(e, a, rel_tol=1e-9, abs_tol=1e-9):
                out.append(f"{p}: {e} became {a}")
        elif e != a:
            out.append(f"{p}: {json.dumps(e)[:60]} became {json.dumps(a)[:60]}")

    walk(expected, actual, path)
    return out


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_scan_matches_its_reference_output(case):
    source = GOLDEN / case.name / "m1.csv.gz"
    if not source.exists():
        write_input(case, make_input(case))
    m1 = read_input(case)
    now = expected_output(case, m1)
    path = GOLDEN / case.name / "expected.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        path.write_text(json.dumps(now, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        if not os.environ.get("UPDATE_GOLDEN"):
            pytest.fail(f"wrote {path.relative_to(GOLDEN.parent)}: review and commit it")
        return
    problems = differences(json.loads(path.read_text(encoding="utf-8")), now)
    assert not problems, (
        f"The scan's output on {case.name} changed. If that's meant, run\n"
        "  UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py\n"
        "and review the diff:\n  " + "\n  ".join(problems))


def test_the_input_is_closed_market_minutes_on_the_feed_clock():
    for case in CASES:
        m1 = read_input(case) if (GOLDEN / case.name / "m1.csv.gz").exists() else make_input(case)
        assert m1.index.is_monotonic_increasing and m1.index.is_unique
        assert _market_hours(m1.index, case.clock).all()
        assert (m1["high"] >= m1[["open", "close"]].max(axis=1)).all()
        assert (m1["low"] <= m1[["open", "close"]].min(axis=1)).all()
