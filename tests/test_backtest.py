import pandas as pd
import pytest

from wednesday.detectors import DetectorParams
from wednesday.feeds import SyntheticFeed
from wednesday.lab import backtest as bt
from wednesday.lab.dataset import unix
from wednesday.lab.outcome import TradePlan, plan_levels, simulate, simulate_trade
from wednesday.timeframes import TIMEFRAMES_BY_NAME

M15 = TIMEFRAMES_BY_NAME["15M"]
PLAN = TradePlan(rr=2.0, horizon_minutes=24 * 60, max_sl=3.0)


@pytest.fixture(scope="module")
def m1():
    return SyntheticFeed(seed=11, history=20_000, end=pd.Timestamp("2026-03-20"))._bars


@pytest.fixture(scope="module")
def blocks(m1):
    return bt.detect_blocks(m1, M15, DetectorParams())


def test_trades_match_simulate_and_start_at_the_break_close(m1, blocks):
    assert len(blocks) > 20
    result = bt.run(m1, M15, blocks, PLAN, "UTC")
    trades = {t["time_unix"]: t for t in result["trades"]}
    assert len(trades) == len(result["trades"])
    for b in blocks:
        t = trades[unix(b.time)]
        entry, stop = plan_levels(b.direction, b.top, b.bottom, PLAN.max_sl)
        outcome, r = simulate(m1, b.direction, entry, stop, b.known, PLAN)  # the Lab's own simulation
        assert (t["outcome"], t["r"]) == (outcome, r)
        assert t["start_unix"] == unix(b.known) and t["start_unix"] > t["time_unix"]
        assert (t["entry"], t["stop"]) == (entry, stop)
    s = result["summary"]
    assert s["trades"] == len(blocks) and s["finished"] == s["wins"] + sum(t["outcome"] == "loss" for t in result["trades"])
    assert s["total_r"] == pytest.approx(sum(t["r"] for t in result["trades"] if t["r"] is not None))
    assert result["equity"] and result["equity"][-1][1] == pytest.approx(s["total_r"])
    assert [p[0] for p in result["equity"]] == sorted(p[0] for p in result["equity"])
    for key in bt.SPLITS:
        assert sum(g["trades"] for g in result["splits"][key]) == s["trades"]
    assert {g["key"] for g in result["splits"]["session"]} <= {"Tokyo", "London", "NY AM", "NY PM"}


def test_no_trade_uses_bars_before_its_start(m1, blocks):
    """Wiping every bar before a block's break close can't change its trade."""
    b = next(x for x in blocks if x.known > m1.index[5000])
    entry, stop = plan_levels(b.direction, b.top, b.bottom, PLAN.max_sl)
    full = simulate_trade(m1, b.direction, entry, stop, b.known, PLAN)
    later = m1[m1.index >= b.known]
    assert simulate_trade(later, b.direction, entry, stop, b.known, PLAN) == full


def test_simulate_trade_exit_time():
    idx = pd.date_range("2026-03-02", periods=6, freq="1min")
    #            start   fill    nothing  target
    lows = [101.5, 99.8, 100.5, 101.0, 101.0, 101.0]
    highs = [102.0, 101.0, 101.5, 102.0, 104.5, 104.0]
    m1 = pd.DataFrame({"open": highs, "high": highs, "low": lows, "close": lows}, index=idx)
    outcome, r, exit_at = simulate_trade(m1, "bullish", 100.0, 98.0, idx[0], TradePlan(rr=2.0, horizon_minutes=60))
    assert (outcome, r, exit_at) == ("win", 2.0, idx[4])
    assert simulate(m1, "bullish", 100.0, 98.0, idx[0], TradePlan(rr=2.0, horizon_minutes=60)) == ("win", 2.0)


def test_summary_drawdown():
    trades = [{"outcome": o, "r": r, "exit_unix": i} for i, (o, r) in
              enumerate([("win", 2.0), ("loss", -1.0), ("loss", -1.0), ("loss", -1.0), ("win", 2.0)])]
    trades.append({"outcome": "untouched", "r": None, "exit_unix": None})
    s = bt.summarize(trades)
    assert s["trades"] == 6 and s["filled"] == 5 and s["finished"] == 5
    assert s["win_rate"] == pytest.approx(0.4) and s["avg_r"] == pytest.approx(0.2)
    assert s["max_dd_r"] == pytest.approx(3.0) and s["total_r"] == pytest.approx(1.0)
    assert bt.summarize([])["fill_rate"] is None


def test_filters(m1, blocks):
    b = blocks[3]
    tag = "ob_bull" if b.direction == "bullish" else "ob_bear"
    labels = [{"tag": tag, "value": 1, "start": unix(b.time), "end": unix(b.time)},
              {"tag": tag, "value": 0, "start": unix(blocks[5].time), "end": unix(blocks[5].time)}]
    mine = bt.run(m1, M15, blocks, PLAN, "UTC", filter="labels", labels=labels)
    assert [t["time_unix"] for t in mine["trades"]] == [unix(b.time)]
    window = bt.run(m1, M15, blocks, PLAN, "UTC", start=blocks[10].known, end=blocks[20].known)
    assert window["summary"]["trades"] == sum(blocks[10].known <= x.known <= blocks[20].known for x in blocks)
    with pytest.raises(ValueError):
        bt.run(m1, M15, blocks, PLAN, "UTC", filter="nope")
    with pytest.raises(ValueError, match="active model"):
        bt.run(m1, M15, blocks, PLAN, "UTC", filter="model")
    csv = bt.trades_csv(mine["trades"]).splitlines()
    assert csv[0].startswith("time,start,exit,direction") and len(csv) == 2
