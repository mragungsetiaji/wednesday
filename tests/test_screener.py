import numpy as np
import pandas as pd
import pytest

from xau_screener.feeds import M1Buffer, SyntheticFeed
from xau_screener.orderblock import detect_order_blocks
from xau_screener.scanner import ScanConfig, scan, split_by_price
from xau_screener.timeframes import TIMEFRAMES_BY_NAME, parse_timeframes, resample_ohlcv


def candles(rows, start="2026-01-05 00:00", freq="5min"):
    """rows: list of (open, high, low, close)."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    return df


def test_parse_timeframes_sorted_high_to_low():
    assert [tf.name for tf in parse_timeframes("5m,4h,15M")] == ["4H", "15M", "5M"]
    with pytest.raises(ValueError):
        parse_timeframes("2H")


def test_resample_aggregates_and_drops_incomplete():
    idx = pd.date_range("2026-01-05 00:00", periods=12, freq="1min")
    m1 = pd.DataFrame({
        "open": np.arange(12.0), "high": np.arange(12.0) + 1, "low": np.arange(12.0) - 1,
        "close": np.arange(12.0) + 0.5, "volume": 1.0,
    }, index=idx)
    out = resample_ohlcv(m1, TIMEFRAMES_BY_NAME["5M"])
    # 00:00-00:04 and 00:05-00:09 are closed; 00:10 bar only has 2 minutes.
    assert list(out.index.strftime("%H:%M")) == ["00:00", "00:05"]
    first = out.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (0.0, 5.0, -1.0, 4.5, 5.0)
    assert len(resample_ohlcv(m1, TIMEFRAMES_BY_NAME["5M"], drop_incomplete=False)) == 3


def test_resample_keeps_last_bar_when_exactly_closed():
    idx = pd.date_range("2026-01-05 00:00", periods=10, freq="1min")
    m1 = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
    assert len(resample_ohlcv(m1, TIMEFRAMES_BY_NAME["5M"])) == 2


def _bullish_bos_series():
    # Rally to a swing high (idx 3), pull back to a low (idx 6), then break above.
    return candles([
        (100, 101, 99, 100.5),
        (100.5, 102, 100, 101.5),
        (101.5, 103, 101, 102.5),
        (102.5, 105, 102, 104),    # swing high 105
        (104, 104.5, 102, 102.5),
        (102.5, 103, 100.5, 101),
        (101, 101.5, 98, 99),      # lowest candle in the leg -> bullish OB 98-101.5
        (99, 102, 98.5, 101.5),
        (101.5, 104, 101, 103.5),
        (103.5, 107, 103, 106.5),  # close 106.5 > 105: bullish BOS
        (106.5, 108, 106, 107.5),
        (107.5, 108.5, 106.5, 108),
    ])


def test_detects_bullish_order_block():
    df = _bullish_bos_series()
    blocks = detect_order_blocks(df, swing_length=2)
    bull = [b for b in blocks if b.kind == "bullish"]
    assert len(bull) == 1
    ob = bull[0]
    assert (ob.bottom, ob.top) == (98, 101.5)
    assert ob.time == df.index[6]
    assert ob.break_time == df.index[9]
    assert not ob.mitigated

    body = detect_order_blocks(df, swing_length=2, zone="body")
    assert [(b.bottom, b.top) for b in body if b.kind == "bullish"] == [(99, 101)]


def test_bullish_order_block_mitigated_by_close_below():
    df = _bullish_bos_series()
    extra = candles([(108, 108, 100, 101), (101, 101.5, 97, 97.5)], start=df.index[-1] + pd.Timedelta("5min"))
    blocks = detect_order_blocks(pd.concat([df, extra]), swing_length=2)
    ob = next(b for b in blocks if b.kind == "bullish")
    assert ob.touches == 1  # first extra candle wicks into 98-101.5
    assert ob.mitigated_time == extra.index[1]


def test_detects_bearish_order_block_as_mirror():
    bull = _bullish_bos_series()
    # Mirror prices around 200: highs become lows and vice versa.
    bear = pd.DataFrame({
        "open": 200 - bull["open"], "high": 200 - bull["low"], "low": 200 - bull["high"],
        "close": 200 - bull["close"], "volume": 1.0,
    }, index=bull.index)
    obs = [b for b in detect_order_blocks(bear, swing_length=2) if b.kind == "bearish"]
    assert len(obs) == 1
    assert (obs[0].bottom, obs[0].top) == (98.5, 102)


def test_no_lookahead_pivot_needs_right_side_bars():
    df = _bullish_bos_series().iloc[:5]  # swing high at idx 3 cannot be confirmed yet
    assert detect_order_blocks(df, swing_length=2) == []


def test_split_by_price():
    df = _bullish_bos_series()
    ob = detect_order_blocks(df, swing_length=2)[0]
    above, below, inside = split_by_price([ob], 110)
    assert above is None and below is ob and inside == []
    above, below, inside = split_by_price([ob], 100)
    assert above is None and below is None and inside == [ob]
    above, below, inside = split_by_price([ob], 90)
    assert above is ob and below is None


def test_scan_end_to_end_with_synthetic_feed():
    cfg = ScanConfig(lookback=100, swing_length=3)
    feed = SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 500, end=pd.Timestamp("2026-03-02 12:00"))
    buf = M1Buffer(feed, max_bars=cfg.required_m1_bars())
    m1 = buf.update()
    assert len(m1) == cfg.required_m1_bars()
    last = m1.index[-1]
    m1 = buf.update()
    assert m1.index[-1] == last + pd.Timedelta("1min")
    assert len(m1) == cfg.required_m1_bars()

    result = scan(m1, cfg)
    assert [r.timeframe.name for r in result.results] == ["4H", "1H", "30M", "15M", "5M"]
    for r in result.results:
        assert r.candles == 100
        if r.above:
            assert r.above.bottom > result.price and not r.above.mitigated
        if r.below:
            assert r.below.top < result.price and not r.below.mitigated
    assert any(r.active for r in result.results)
    assert result.to_dict()["timeframes"][0]["timeframe"] == "4H"
