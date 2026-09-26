import pandas as pd
import pytest

from wednesday.detectors import DetectorParams, parse_detectors
from wednesday.detectors.inducement import InducementDetector
from wednesday.detectors.liquidity import LiquidityDetector
from wednesday.structure import Context, analyze_structure


def candles(rows, start="2026-01-05 00:00", freq="5min"):
    """rows: list of (open, high, low, close)."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    return df


def bar(mid, spread=0.5):
    return (mid, mid + spread, mid - spread, mid)


def test_parse_detectors():
    assert parse_detectors("OB, idm,ob") == ("ob", "idm")
    with pytest.raises(ValueError):
        parse_detectors("fvg")


def test_structure_is_shared_and_cached():
    df = candles([bar(100 + (i % 7)) for i in range(40)])
    ctx = Context(df)
    assert ctx.structure(2) is ctx.structure(2)
    s = analyze_structure(ctx.high, ctx.low, ctx.close, 2)
    assert all(sw.confirmed == sw.index + 2 for sw in s.swings)


def _two_equal_highs():
    # Swing high 110 at idx 3, pullback, second high 109.95 at idx 9, then drop.
    mids = [100, 104, 107, 109.5, 106, 103, 102, 105, 108, 109.45, 106, 103, 101, 100, 99]
    return candles([bar(m) for m in mids])


def test_equal_highs_merge_into_one_bsl_pool():
    df = _two_equal_highs()
    ctx = Context(df)
    levels = LiquidityDetector(DetectorParams(swing_length=2, eq_tolerance=0.5)).detect(ctx)
    bsl = [lv for lv in levels if lv.kind == "bsl" and lv.active]
    assert len(bsl) == 1
    pool = bsl[0]
    assert pool.label == "EQH x2" and pool.meta["equal"] == 2
    assert (pool.bottom, pool.top) == (109.95, 110.0)
    assert pool.time == df.index[3]


def test_equal_highs_stay_separate_with_tight_tolerance():
    levels = LiquidityDetector(DetectorParams(swing_length=2, eq_tolerance=0.01)).detect(Context(_two_equal_highs()))
    bsl = sorted(lv.top for lv in levels if lv.kind == "bsl" and lv.active)
    assert bsl == [109.95, 110.0]


def test_ssl_sweep_is_a_grab_when_close_returns():
    mids = [105, 103, 101, 100, 102, 104, 106, 105, 103, 101.5]
    rows = [bar(m) for m in mids]
    rows.append((101, 101.5, 99.0, 100.2))  # wick below SSL 99.5, close back above
    df = candles(rows)
    levels = LiquidityDetector(DetectorParams(swing_length=2)).detect(Context(df))
    ssl = next(lv for lv in levels if lv.kind == "ssl" and lv.top == 99.5)
    assert ssl.ended_time == df.index[-1]
    assert ssl.meta["grab"] is True


def test_bullish_inducement_after_bos():
    # Swing high 105.5 (idx 3) -> break at idx 8 -> pullback low 104.5 (idx 10) = IDM -> sweep at idx 14.
    mids = [100, 102, 104, 105, 103, 101, 102, 104, 106.5, 107, 105, 106.5, 108, 107.5]
    rows = [bar(m) for m in mids]
    df = candles(rows)
    params = DetectorParams(swing_length=2, idm_length=1)
    (idm,) = InducementDetector(params).detect(Context(df))
    assert idm.kind == "bullish" and idm.label == "BULL IDM"
    assert idm.top == idm.bottom == 104.5
    assert idm.time == df.index[10]
    assert idm.active

    swept = candles(rows + [(107.5, 107.6, 104.0, 105.0)])
    (idm,) = InducementDetector(params).detect(Context(swept))
    assert idm.ended_time == swept.index[-1]
    assert idm.meta["grab"] is True


def _ctx_with_breaks(directions):
    """Context whose structure(2) has the given break directions (synthetic, bypasses detection)."""
    from wednesday.structure import Break, Structure, Swing

    df = candles([bar(100) for _ in range(20)])
    ctx = Context(df)
    breaks = [Break(d, 5 + i * 3, Swing("high" if d == "bullish" else "low", 2 + i * 3, 100.0 + i, 4 + i * 3))
              for i, d in enumerate(directions)]
    ctx._structures[2] = Structure(2, [], breaks)
    return ctx


def test_latest_bias_bos_vs_choch():
    from wednesday.structure import latest_bias

    assert latest_bias(_ctx_with_breaks([]), 2) is None
    b = latest_bias(_ctx_with_breaks(["bullish"]), 2)
    assert (b.direction, b.event, b.streak) == ("bullish", "BOS", 1)
    b = latest_bias(_ctx_with_breaks(["bullish", "bullish", "bearish"]), 2)
    assert (b.direction, b.event, b.streak) == ("bearish", "CHoCH", 1)
    b = latest_bias(_ctx_with_breaks(["bullish", "bearish", "bearish", "bearish"]), 2)
    assert (b.direction, b.event, b.streak) == ("bearish", "BOS", 3)
    assert b.bars_ago == 19 - (5 + 3 * 3)
    assert b.level == 103.0


def test_bias_from_real_structure():
    from wednesday.structure import latest_bias

    mids = [100, 102, 104, 105, 103, 101, 102, 104, 106.5, 107, 105, 106.5, 108, 107.5]
    b = latest_bias(Context(candles([bar(m) for m in mids])), 2)
    # Two bullish breaks: 105.5 (idx 8) then 107.5 (idx 12).
    assert (b.direction, b.event, b.streak, b.level, b.bars_ago) == ("bullish", "BOS", 2, 107.5, 1)
