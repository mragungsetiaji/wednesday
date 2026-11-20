"""Reference data for the journal's numbers (#96): trades, cash and M1 bars in, ``analyse`` out.

Each case in ``tests/golden/journal/<case>/`` has the input, ``input.json`` (the trade and cash rows
as the journal stores them, and the clock offset or null to detect it) and ``bars.csv.gz`` (M1
bars per symbol on the price clock, unix seconds), and ``expected.json``: what
``wednesday.journal.stats.analyse`` returns, as JSON.

The Rust port (``server-rs/tests/journal_golden.rs``) must give exactly this output: the same
keys in the same order, the same numbers to the bit, and an int where Python has an int (``0``)
and a float where it has a float (``0.0``). So the comparison here is on the JSON text.

    UPDATE_GOLDEN=1 uv run pytest tests/test_journal_golden.py

rewrites the expected output after a deliberate change to the stats; review the diff. The input
is cut from the sample portfolio (``journal/sample.py``) and only written when a case's files
are missing.
"""

from __future__ import annotations

import gzip
import io
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wednesday.journal import sample as sample_mod
from wednesday.journal.stats import analyse

GOLDEN = Path(__file__).with_name("golden") / "journal"
HOUR = 3600
JOURNAL = "j-golden"


# ---- the input ----

_SAMPLE: dict | None = None


def _sample() -> dict:
    global _SAMPLE
    if _SAMPLE is None:
        _SAMPLE = sample_mod.generate()
    return _SAMPLE


def _ts(day: str) -> int:
    return int(pd.Timestamp(day).timestamp())


def _window(start: str, end: str) -> tuple[pd.DataFrame, list[dict]]:
    """The sample's bars from ``start`` up to ``end`` (exclusive) and the trades inside them."""
    s = _sample()
    lo, hi = _ts(start), _ts(end)
    bars = s["bars"][(s["bars"].index >= pd.Timestamp(start)) & (s["bars"].index < pd.Timestamp(end))]
    trades = [dict(t) for t in s["trades"] if t["open_time"] >= lo and t["close_time"] < hi]
    return bars[["open", "high", "low", "close"]].copy(), trades


def _row(tr: dict) -> dict:
    """A trade in the stored column order, with its journal id first (``Store.journal_rows``)."""
    keys = ["id", "position", "symbol", "side", "volume", "open_time", "open_price", "close_time", "close_price",
            "profit", "commission", "swap", "sl", "tp", "comment"]
    return {"journal_id": JOURNAL, **{k: tr.get(k) for k in keys}}


def _cash(i: int, when: int, kind: str, amount: float) -> dict:
    return {"journal_id": JOURNAL, "id": f"c{i}", "time": when, "kind": kind, "amount": amount, "comment": None}


def _shift(trades: list[dict], seconds: int) -> list[dict]:
    """The deal clock ``seconds`` ahead of the price clock."""
    return [{**t, "open_time": t["open_time"] + seconds,
             "close_time": t["close_time"] + seconds if t["close_time"] is not None else None} for t in trades]


def _sample_cash(lo: int | None = None, hi: int | None = None, shift: int = 0) -> list[dict]:
    rows = [_cash(i + 1, c["time"] + shift, c["kind"], c["amount"]) for i, c in enumerate(_sample()["cash"])]
    return [c for c in rows if (lo is None or c["time"] >= lo) and (hi is None or c["time"] < hi)]


def _fx(n: int, start: int, open_: bool = False) -> list[dict]:
    """EURUSD trades: a symbol with no bars."""
    out = []
    for i in range(n):
        t0 = start + i * 5 * HOUR
        last = open_ and i == n - 1
        out.append({"id": f"fx{i}", "position": f"9{i}", "symbol": "EURUSD", "side": "buy" if i % 2 else "sell",
                    "volume": 0.5, "open_time": t0, "open_price": 1.0812 + i / 1000,
                    "close_time": None if last else t0 + 2 * HOUR, "close_price": 1.0831 + i / 1000,
                    "profit": round((19 if i % 2 else -19) * 5.0, 2), "commission": -3.5, "swap": -0.4 * i,
                    "sl": None, "tp": None, "comment": "no bars"})
    return out


def _same_minute(bars: pd.DataFrame, at: str, side: str, shift: int = 0) -> dict:
    """A trade opened and closed inside one M1 bar."""
    i = bars.index.get_loc(pd.Timestamp(at))
    t = int(pd.Timestamp(at).timestamp())
    o, lo_, hi = float(bars["open"].iloc[i]), float(bars["low"].iloc[i]), float(bars["high"].iloc[i])
    entry, exit_ = round(lo_ + (hi - lo_) * 0.3, 2), round(lo_ + (hi - lo_) * 0.8, 2)
    sign = 1 if side == "buy" else -1
    return {"id": "same-minute", "position": "77", "symbol": "XAUUSD", "side": side, "volume": 0.2,
            "open_time": t + 7 + shift, "open_price": entry, "close_time": t + 48 + shift, "close_price": exit_,
            "profit": round((exit_ - entry) * sign * 0.2 * 100, 2), "commission": -1.4, "swap": 0.0,
            "sl": round(o - 5, 2), "tp": None, "comment": None}


def _open(tr: dict, profit: float) -> dict:
    """A trade still open: no close time, its last price and floating profit."""
    return {**tr, "close_time": None, "profit": profit, "id": tr["id"] + "-open"}


def _silver(bars: pd.DataFrame) -> pd.DataFrame:
    s = (bars / 80).round(3)
    s["high"] = s[["open", "high", "close"]].max(axis=1)
    s["low"] = s[["open", "low", "close"]].min(axis=1)
    return s


def make_case(name: str) -> tuple[dict, dict[str, pd.DataFrame]]:
    """(input.json content, bars per symbol) of a case."""
    if name == "sample-3w":
        bars, trades = _window("2026-03-02", "2026-03-21")
        return {"trades": [_row(t) for t in trades], "cash": _sample_cash(hi=_ts("2026-03-21")), "offset": None}, \
            {"XAUUSD": bars}

    if name in ("broker-plus3", "explicit-offset-open"):
        bars, trades = _window("2026-03-09", "2026-03-21")
        trades = _shift(trades, 3 * HOUR)
        trades += _fx(3, _ts("2026-03-10 08:00"), open_=name == "explicit-offset-open")
        cash = _sample_cash(shift=3 * HOUR) + [
            _cash(10, _ts("2026-03-11 10:00"), "withdrawal", -400.0),
            _cash(11, _ts("2026-03-12 10:00"), "credit", 150.0),
            _cash(12, _ts("2026-03-13 10:00"), "other", -12.5),
        ]
        offset = None
        if name == "explicit-offset-open":
            offset = 3 * HOUR
            trades.append(_same_minute(bars, "2026-03-16 13:21", "buy", shift=3 * HOUR))
            xau = [i for i, t in enumerate(trades) if t["symbol"] == "XAUUSD"][-4:-1]
            for i, gain in zip(xau, (35.5, -120.25, 0.0)):
                trades[i] = _open(trades[i], gain)
            # opened after the last bar: no bars for it
            trades.append(_open({**trades[xau[0]], "id": "late", "open_time": _ts("2026-03-21 04:00") + 3 * HOUR}, 4.0))
            trades.sort(key=lambda t: t["open_time"])
        return {"trades": [_row(t) for t in trades], "cash": cash, "offset": offset}, {"XAUUSD": bars}

    if name == "mismatched":
        bars, trades = _window("2026-03-02", "2026-03-14")
        for i, t in enumerate(trades):
            if i % 5 == 2:
                t["open_price"] = round(t["open_price"] + 8.0, 2)  # off its bar
            if i % 11 == 4:
                t["close_price"] = t["open_price"] = round((t["open_price"] + t["close_price"]) / 2, 2)
                t["profit"] = 0.0  # flat: zero pips, nothing to learn a multiplier from
        silver = _silver(bars)
        ag = []
        for i, t in enumerate(trades[::3]):
            o, c = round(t["open_price"] / 80, 3), round((t["close_price"] or t["open_price"]) / 80, 3)
            sign = 1 if t["side"] == "buy" else -1
            ag.append({**t, "id": f"ag{i}", "symbol": "XAGUSD", "open_price": o, "close_price": c,
                       "profit": round((c - o) * sign * t["volume"] * 5000 * (-1 if i == 2 else 1), 2)})
        trades = sorted(trades + ag, key=lambda t: t["open_time"])
        cash = _sample_cash(hi=_ts("2026-03-14")) + [_cash(9, _ts("2026-03-06 12:00"), "credit", 250.0)]
        return {"trades": [_row(t) for t in trades], "cash": cash, "offset": None}, \
            {"XAUUSD": bars, "XAGUSD": silver}

    if name == "sell-heavy-minus2":
        bars, trades = _window("2026-03-23", "2026-04-11")
        buys = [t for t in trades if t["side"] == "buy"][:2]
        trades = sorted([t for t in trades if t["side"] == "sell"] + buys, key=lambda t: t["open_time"])
        trades = _shift(trades, -2 * HOUR)
        return {"trades": [_row(t) for t in trades], "cash": _sample_cash(), "offset": None}, {"XAUUSD": bars}

    if name == "no-offset-match":
        bars, trades = _window("2026-03-02", "2026-03-07")
        for t in trades:
            t["open_price"] = round(t["open_price"] + 40, 2)
            t["close_price"] = round(t["close_price"] + 40, 2)
        return {"trades": [_row(t) for t in trades], "cash": _sample_cash(hi=_ts("2026-03-07")), "offset": None}, \
            {"XAUUSD": bars}

    if name == "cash-only":
        bars, _ = _window("2026-03-02", "2026-03-03")
        cash = [_cash(1, _ts("2026-03-02 00:30"), "deposit", 1000.0), _cash(2, _ts("2026-03-02 05:00"), "credit", 50.0),
                _cash(3, _ts("2026-04-02 05:00"), "other", 0.25)]
        return {"trades": [], "cash": cash, "offset": None}, {"XAUUSD": bars}

    if name == "offset-tie":
        # flat bars at 2000 except 10:00-11:00 and 14:00-15:00 (2200) and 12:00-13:00 (2100): deals at
        # 12:xx priced 2000 match a shift of -1h and +1h equally, and the tie goes to -1h
        t = pd.date_range("2026-05-06", periods=1440, freq="min")
        price = np.where(t.hour == 12, 2100.0, np.where(np.isin(t.hour, (10, 14)), 2200.0, 2000.0))
        bars = pd.DataFrame({"open": price, "high": price, "low": price, "close": price}, index=t.as_unit("s"))
        trades = [{"id": f"tie{i}", "position": str(i), "symbol": "XAUUSD", "side": "buy", "volume": 0.1,
                   "open_time": _ts(f"2026-05-06 12:{5 + 10 * i:02d}:13"), "open_price": 2000.0,
                   "close_time": _ts(f"2026-05-06 12:{9 + 10 * i:02d}:41"), "close_price": 2000.2,
                   "profit": 2.0, "commission": 0.0, "swap": 0.0, "sl": None, "tp": None, "comment": None}
                  for i in range(3)]
        cash = [_cash(1, _ts("2026-05-06 00:10"), "deposit", 1000.0)]
        return {"trades": [_row(t) for t in trades], "cash": cash, "offset": None}, {"XAUUSD": bars}

    if name == "edges-rounding":
        # bars with a gap from 10:00 to 10:09: a trade opened 290 s after the last bar before it is
        # verified, one opened 330 s after is not (EDGE = 300)
        t = pd.date_range("2026-05-04", periods=1440, freq="min")
        t = t[(t < pd.Timestamp("2026-05-04 10:00")) | (t >= pd.Timestamp("2026-05-04 10:10"))]
        price = 2000 + (np.arange(len(t)) % 7) * 0.1
        bars = pd.DataFrame({"open": price, "high": price + 0.3, "low": price - 0.3, "close": price},
                            index=t.as_unit("s")).round(2)

        def gold(i, opened, closed, entry, exit_, side="buy"):
            sign = 1 if side == "buy" else -1
            return {"id": f"edge{i}", "position": str(i), "symbol": "XAUUSD", "side": side, "volume": 0.1,
                    "open_time": _ts(opened), "open_price": entry, "close_time": _ts(closed), "close_price": exit_,
                    "profit": round((exit_ - entry) * sign * 10, 2), "commission": -0.7, "swap": 0.0,
                    "sl": None, "tp": None, "comment": None}

        def fx(i, closed, profit):
            # no bars: closed result only; the nets are picked so Python's round() and numpy's differ
            return {"id": f"round{i}", "position": str(100 + i), "symbol": "EURUSD", "side": "buy", "volume": 0.01,
                    "open_time": _ts(closed) - 600, "open_price": 1.1, "close_time": _ts(closed), "close_price": 1.1001,
                    "profit": profit, "commission": 0.0, "swap": 0.0, "sl": None, "tp": None, "comment": None}

        trades = [
            fx(1, "2026-05-04 01:00", 3.43),  # growth 0.42874999...: numpy rounds to 0.4288, Python 0.4287
            gold(2, "2026-05-04 10:03:50", "2026-05-04 10:30:10", 2000.25, 2000.4),
            gold(3, "2026-05-04 10:04:30", "2026-05-04 10:40:10", 2000.25, 2000.1, "sell"),
            fx(4, "2026-05-05 09:00", 0.005),  # round(0.005, 2) is 0.01 in Python, 0.0 in numpy
        ]
        cash = [_cash(1, _ts("2026-05-04 00:30"), "deposit", 800.0)]
        return {"trades": [_row(t) for t in trades], "cash": cash, "offset": 0}, {"XAUUSD": bars}

    if name == "empty":
        return {"trades": [], "cash": [], "offset": None}, {}

    raise ValueError(name)


CASES = ["sample-3w", "broker-plus3", "explicit-offset-open", "mismatched", "sell-heavy-minus2",
         "no-offset-match", "cash-only", "empty", "offset-tie", "edges-rounding"]


def write_input(name: str) -> None:
    inp, bars = make_case(name)
    d = GOLDEN / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "input.json").write_text(json.dumps(inp, indent=1) + "\n", encoding="utf-8")
    lines = ["symbol,time,open,high,low,close"]
    for sym, df in bars.items():
        t = df.index.as_unit("s").asi8
        for i in range(len(df)):
            lines.append(",".join([sym, str(int(t[i]))] + [repr(float(df[c].iloc[i])) for c in ("open", "high", "low", "close")]))
    with gzip.GzipFile(d / "bars.csv.gz", "wb", mtime=0) as f:
        f.write(("\n".join(lines) + "\n").encode())


def read_input(name: str) -> tuple[dict, dict[str, pd.DataFrame]]:
    d = GOLDEN / name
    inp = json.loads((d / "input.json").read_text(encoding="utf-8"))
    with gzip.open(d / "bars.csv.gz", "rt") as f:
        text = f.read()
    rows = [line.split(",") for line in text.splitlines()[1:] if line]
    by_symbol: dict[str, list] = {}
    for r in rows:
        by_symbol.setdefault(r[0], []).append((int(r[1]), *map(float, r[2:])))
    bars = {}
    for sym, rs in by_symbol.items():
        a = np.array([r[0] for r in rs], dtype=np.int64)
        bars[sym] = pd.DataFrame({c: [r[i + 1] for r in rs] for i, c in enumerate(("open", "high", "low", "close"))},
                                 index=pd.DatetimeIndex(pd.to_datetime(a, unit="s")).as_unit("s"))
    return inp, bars


def render(result: dict) -> str:
    return json.dumps(result, indent=1, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("name", CASES)
def test_the_journal_stats_match_their_reference_output(name):
    d = GOLDEN / name
    if not (d / "input.json").exists() or not (d / "bars.csv.gz").exists():
        write_input(name)
    inp, bars = read_input(name)
    now = render(analyse(inp["trades"], inp["cash"], bars, inp["offset"]))
    path = d / "expected.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        path.write_text(now, encoding="utf-8")
        if not os.environ.get("UPDATE_GOLDEN"):
            pytest.fail(f"wrote the missing {path}; review it and run again")
    assert now == path.read_text(encoding="utf-8"), (
        f"journal stats for {name} changed; if deliberate, review and rewrite:\n"
        "  UPDATE_GOLDEN=1 uv run pytest tests/test_journal_golden.py")


def test_the_cases_cover_what_they_say():
    """Each case still exercises what it was made for."""
    out = {n: json.loads((GOLDEN / n / "expected.json").read_text()) for n in CASES if (GOLDEN / n / "expected.json").exists()}
    if len(out) < len(CASES):
        pytest.skip("expected output not written yet")
    v = {n: o["verification"] for n, o in out.items()}
    assert v["sample-3w"]["offset_hours"] == 0.0 and v["sample-3w"]["verified"] > 20
    assert v["broker-plus3"]["offset_hours"] == 3.0 and v["broker-plus3"]["offset_detected"]
    assert v["broker-plus3"]["no_prices"] == ["EURUSD"]
    assert v["explicit-offset-open"]["offset_detected"] is False and out["explicit-offset-open"]["trading"]["open"] >= 4
    assert any(t["id"] == "same-minute" and t["verified"] for t in out["explicit-offset-open"]["trades"])
    assert v["mismatched"]["mismatched"] and v["mismatched"]["offset_hours"] == 0.0
    assert v["sell-heavy-minus2"]["offset_hours"] == -2.0 and len(out["sell-heavy-minus2"]["monthly"]) >= 3
    assert v["no-offset-match"]["offset_hours"] is None
    assert v["offset-tie"]["offset_hours"] == -1.0
    edges = {t["id"]: t["verified"] for t in out["edges-rounding"]["trades"]}
    assert edges["edge2"] and not edges["edge3"]
    assert out["edges-rounding"]["series"]["growth"][1] == [_ts("2026-05-04 01:00"), 0.4288]
    assert out["edges-rounding"]["daily"][-1]["profit"] == 0.01
    assert out["empty"]["trading"]["trades"] == 0 and out["cash-only"]["summary"]["profit"] == 0
