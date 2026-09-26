import pandas as pd

from xau_screener.detectors import DetectorParams
from xau_screener.detectors.orderblock import OrderBlockDetector
from xau_screener.levels import Level
from xau_screener.scanner import LevelSet, ScanResult, TimeframeResult
from xau_screener.structure import Context
from xau_screener.timeframes import TIMEFRAMES_BY_NAME


def candles(rows, start="2026-01-05 00:00", freq="5min"):
    """rows: list of (open, high, low, close)."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    return df


def detect(df, **kw):
    return OrderBlockDetector(DetectorParams(swing_length=2, **kw)).detect(Context(df))


def mirror(df):
    """Mirror prices around 200: bullish structure becomes bearish, red candles become green."""
    return pd.DataFrame({
        "open": 200 - df["open"], "high": 200 - df["low"], "low": 200 - df["high"],
        "close": 200 - df["close"], "volume": 1.0,
    }, index=df.index)


RALLY = [
    (100, 101, 99, 100.5),
    (100.5, 102, 100, 101.5),
    (101.5, 103, 101, 102.5),
    (102.5, 110, 102, 109),       # 3: swing high 110
    (109, 109.5, 105, 105.5),     # 4: red
    (105.5, 106, 100.5, 101),     # 5: red
    (101, 101.5, 98, 99),         # 6: red at the lowest low -> extreme OB, body 99-101
    (99, 103, 98.5, 102.5),       # 7: green
    (102.5, 103, 101.5, 102),     # 8: red, then impulsive green -> middle OB, body 102-102.5
    (102, 106, 101.8, 105.5),     # 9: green closing above candle 8's body
    (105.5, 111, 105, 110.5),     # 10: close 110.5 > 110 -> bullish BOS
    (110.5, 112, 110, 111.5),
    (111.5, 113, 111, 112.5),
]


def test_extreme_and_middle_bullish_obs():
    df = candles(RALLY)
    obs = detect(df)
    assert [(ob.time, ob.meta["priority"]) for ob in obs] == [(df.index[6], "extreme"), (df.index[8], "middle")]
    ext, mid = obs
    assert ext.label == "BULL OB" and mid.label == "BULL OB (mid)"
    # Buy limit at the top of the red body, stop at its bottom.
    assert (ext.bottom, ext.top) == (99, 101)
    assert (ext.meta["entry"], ext.meta["sl"], ext.meta["risk"]) == (101, 99, 2)
    assert ext.meta["sl_capped"] is False
    assert ext.confirmed_time == df.index[10]
    assert (mid.meta["entry"], mid.meta["sl"]) == (102.5, 102)
    assert all(ob.active for ob in obs)


def test_zone_wick_keeps_body_entry():
    (ext, _) = detect(candles(RALLY), zone="wick")
    assert (ext.bottom, ext.top) == (98, 101.5)
    assert (ext.meta["entry"], ext.meta["sl"]) == (101, 99)


def test_stop_is_capped_at_max_sl():
    rows = list(RALLY)
    rows[6] = (104, 104.5, 98, 99)  # body 99-104 = 5 points
    ext = detect(candles(rows))[0]
    assert (ext.meta["entry"], ext.meta["sl"], ext.meta["risk"]) == (104, 101, 3)
    assert ext.meta["sl_capped"] is True
    assert ext.meta["body"] == 5
    ext = detect(candles(rows), max_sl=10)[0]
    assert ext.meta["sl"] == 99


def test_touching_entry_is_not_taken_but_wick_through_body_is():
    df = candles(RALLY)
    after = df.index[-1] + pd.Timedelta("5min")
    touch = candles([(112, 112.5, 100.5, 110)], start=after)  # wick to 100.5: into the extreme body, not through
    ext = detect(pd.concat([df, touch]))[0]
    assert ext.active and ext.touches == 1

    wipe = candles([(112, 112.5, 100.5, 110), (110, 110.5, 98.9, 108)], start=after)  # wick below 99
    ext = detect(pd.concat([df, wipe]))[0]
    assert ext.ended_time == wipe.index[1]
    assert ext.touches == 1


def test_close_mitigation_needs_close_beyond_body():
    df = candles(RALLY)
    wick_only = candles([(112, 112.5, 98, 110)], start=df.index[-1] + pd.Timedelta("5min"))
    ext = detect(pd.concat([df, wick_only]), mitigation="close")[0]
    assert ext.active


def test_bearish_is_the_mirror():
    df = mirror(candles(RALLY))
    obs = detect(df)
    assert [ob.kind for ob in obs] == ["bearish", "bearish"]
    ext = obs[0]
    assert ext.label == "BEAR OB" and ext.meta["priority"] == "extreme"
    # Sell limit at the bottom of the green body, stop at its top.
    assert (ext.bottom, ext.top) == (99, 101)
    assert (ext.meta["entry"], ext.meta["sl"]) == (99, 101)


def _ob(kind, entry, priority, sl_offset=1.0):
    t = pd.Timestamp("2026-01-05")
    top, bottom = (entry, entry - sl_offset) if kind == "bullish" else (entry + sl_offset, entry)
    return Level("ob", kind, top, bottom, t, t, "OB",
                 meta={"priority": priority, "entry": entry, "sl": bottom if kind == "bullish" else top})


def _result(price, per_tf):
    results = []
    for name, levels in per_tf.items():
        s = LevelSet(levels, None, None, [], [])
        results.append(TimeframeResult(TIMEFRAMES_BY_NAME[name], 100, {"ob": s}))
    return ScanResult(pd.Timestamp("2026-01-05"), price, ("ob",), results)


def test_setups_rank_extreme_before_nearer_middle():
    res = _result(100, {
        "1H": [_ob("bearish", 110, "extreme"), _ob("bullish", 90, "extreme")],
        "5M": [_ob("bearish", 102, "middle"), _ob("bearish", 105, "extreme"), _ob("bullish", 97, "middle"),
               _ob("bearish", 99.5, "extreme")],  # price already past this entry: not a limit setup
    })
    sell = [(tf.name, lv.meta["entry"], round(d, 2)) for tf, lv, d in res.setups("sell")]
    assert sell == [("5M", 105, 5), ("1H", 110, 10), ("5M", 102, 2)]
    buy = [(tf.name, lv.meta["entry"]) for tf, lv, _ in res.setups("buy")]
    assert buy == [("1H", 90), ("5M", 97)]
    d = res.to_dict()["setups"]
    assert d["sell"][0]["timeframe"] == "5M" and d["sell"][0]["distance"] == 5
