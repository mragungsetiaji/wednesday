"""Optional feature families (issue #10): causal, recorded in the manifest, rebuilt for prediction."""

import pandas as pd
import pytest

from wednesday.detectors import DetectorParams
import numpy as np

from wednesday.lab import dataset
from wednesday.lab.dataset import FAMILIES, STRUCTURE_LENGTH, FeatureParams, candle_features, higher_candles
from wednesday.structure import analyze_structure
from wednesday.lab.explain import describe
from wednesday.lab.model import load_bytes
from wednesday.lab.train import TrainParams, predict_blocks, train_bundle
from wednesday.timeframes import TIMEFRAMES_BY_NAME, resample_ohlcv

from test_lab import detector_labels, quick_models, synthetic  # noqa: F401 - the fixture is used by name

M5 = TIMEFRAMES_BY_NAME["5M"]
Q = ["q_session", "q_q90", "q_weekday", "q_week_move", "q_session_move"]


def test_quarter_features_are_off_unless_asked():
    c = resample_ohlcv(synthetic(3000), M5)
    assert not any(k.startswith("q_") for k in candle_features(c, M5, FeatureParams()).columns)
    assert set(Q) <= set(candle_features(c, M5, FeatureParams(families=("quarters",))).columns)


def test_quarter_features_never_look_ahead():
    c = resample_ohlcv(synthetic(6000), M5)
    fp = FeatureParams(lookback=5, confirm=2, families=("quarters",), clock="NY+7")
    base = candle_features(c, M5, fp)
    i = 400
    changed = c.copy()
    changed.iloc[i + fp.confirm + 1:, :4] *= 1.05  # everything after the moment candle i is judged
    again = candle_features(changed, M5, fp)
    t = c.index[i]
    pd.testing.assert_series_equal(base.loc[t, Q], again.loc[t, Q])


def test_session_and_quarter_in_new_york_time():
    # Feed clock NY+7: 16:30 on the feed is 09:30 New York, NY AM (06:00-12:00), its third quarter (09:00-10:30).
    idx = pd.date_range("2026-03-04 16:30", periods=3, freq="5min")
    c = pd.DataFrame({"open": [10.0, 11, 12], "high": [11.0, 12, 13], "low": [9.0, 10, 11], "close": [11.0, 12, 13]}, index=idx)
    f = candle_features(c, M5, FeatureParams(lookback=0, confirm=0, atr_length=1, families=("quarters",), clock="NY+7"))
    row = f.iloc[0]
    assert (row["q_session"], row["q_q90"], row["q_weekday"]) == (2.0, 2.0, 2.0)  # NY AM, Q3, Wednesday
    assert f["q_session_move"].iloc[-1] > 0  # closed above the session's first open


def test_every_quarter_feature_has_a_readable_name():
    for name in Q:
        family, label = describe(name)
        assert family == "quarters" and label != name


def test_manifest_lists_the_families_and_prediction_builds_them():
    m1 = synthetic(9000)
    tf = TIMEFRAMES_BY_NAME["15M"]
    labels, reviewed = detector_labels(m1, tf, m1.index[-2000])
    params = TrainParams(timeframes=["15M"], tags=["ob_bull", "bsl", "ssl"], families=["quarters"], clock="NY+7")
    assert params.validate() == []
    bundle = load_bytes(train_bundle(m1, {"15M": labels}, {"15M": reviewed}, params, "XAU", DetectorParams()).to_bytes())
    assert bundle.manifest["params"]["families"] == ["quarters"] and bundle.manifest["params"]["clock"] == "NY+7"
    assert set(Q) <= set(bundle.features)
    assert FeatureParams.of(bundle.manifest["params"]).families == ("quarters",)
    assert predict_blocks(bundle, m1, tf, limit=100)  # the columns it was trained on are there


def test_unknown_family_is_refused():
    assert TrainParams(families=["astrology"]).validate()
    assert set(FAMILIES) == {"quarters", "structure", "liquidity", "news", "bias"}


@pytest.mark.parametrize("params", [{"lookback": 10, "confirm": 3}, {"lookback": 4, "confirm": 1, "families": None}])
def test_models_from_before_families_build_the_old_features(params):
    assert FeatureParams.of(params).families == ()


S = ["s_break_dir", "s_break_choch", "s_since_break", "s_swing_label", "s_dist_high", "s_dist_low",
     "s_htf_break_dir", "s_htf_since_break"]
L = ["l_swept_high", "l_swept_low", "l_dist_eq_high", "l_dist_eq_low"]


@pytest.mark.parametrize("family,cols", [("structure", S), ("liquidity", L)])
def test_structure_and_liquidity_never_look_ahead(family, cols):
    m1 = synthetic(9000)
    c = resample_ohlcv(m1, M5)
    fp = FeatureParams(lookback=5, confirm=2, families=(family,))
    base = candle_features(c, M5, fp, higher_candles(m1, M5))
    i = 900
    t = c.index[i]
    cut = c.index[i + fp.confirm] + M5.delta  # available_at
    changed = m1.copy()
    changed.loc[changed.index >= cut, ["open", "high", "low", "close"]] *= 1.03
    c2 = resample_ohlcv(changed, M5)
    again = candle_features(c2, M5, fp, higher_candles(changed, M5))
    pd.testing.assert_series_equal(base.loc[t, cols], again.loc[t, cols])
    assert base[cols].notna().any().all()  # every column says something somewhere


def test_structure_matches_the_detector_structure():
    c = resample_ohlcv(synthetic(6000), M5)
    fp = FeatureParams(lookback=5, confirm=2, families=("structure",))
    f = candle_features(c, M5, fp)
    st = analyze_structure(c["high"].to_numpy(), c["low"].to_numpy(), c["close"].to_numpy(), STRUCTURE_LENGTH)
    for b in st.breaks[5:15]:
        k = b.index  # the break is known at its own close: candidate k - confirm sees it
        row = f.loc[c.index[k - fp.confirm]]
        assert row["s_break_dir"] == (1.0 if b.direction == "bullish" else -1.0)
        assert row["s_since_break"] == 0


def test_news_minutes_and_unknown_outside_the_calendar():
    idx = pd.date_range("2026-03-04 13:00", periods=4, freq="5min")  # feed clock UTC
    c = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}, index=idx)
    release = int(pd.Timestamp("2026-03-04 13:30", tz="UTC").timestamp())
    try:
        dataset.use_news(lambda: ([release - 86400, release], release - 86400, release))
        f = candle_features(c, M5, FeatureParams(lookback=0, confirm=0, atr_length=1, families=("news",)))
        assert f["n_to_next"].tolist()[:2] == [30.0, 25.0]
        assert f["n_since_last"].iloc[0] == 24 * 60 - 30
        dataset.use_news(lambda: ([release], release + 30 * 86400, release + 40 * 86400))  # calendar starts later
        f = candle_features(c, M5, FeatureParams(lookback=0, confirm=0, atr_length=1, families=("news",)))
        assert f["n_to_next"].isna().all()
    finally:
        dataset.use_news(None)


def test_every_new_feature_has_a_readable_name():
    for name in S + L + ["n_to_next", "n_since_last"]:
        family, label = describe(name)
        assert family in {"structure", "liquidity", "news"} and label != name, name


def test_bias_in_force_when_the_candle_is_judged():
    idx = pd.date_range("2026-03-04 12:00", periods=6, freq="1h")  # feed clock UTC; judged at each close
    c = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}, index=idx)
    h1 = TIMEFRAMES_BY_NAME["1H"]
    history = [
        {"set_at": "2026-03-04T13:30:00+00:00", "direction": "bullish", "expires_at": "2026-03-04T15:30:00+00:00"},
        {"set_at": "2026-03-04T16:10:00+00:00", "direction": "neutral", "expires_at": None},
        {"set_at": "2026-03-04T16:40:00+00:00", "direction": None, "expires_at": None},  # cleared
    ]
    try:
        dataset.use_bias(lambda: history)
        f = candle_features(c, h1, FeatureParams(lookback=0, confirm=0, atr_length=1, families=("bias",)))
    finally:
        dataset.use_bias(None)
    # judged at 13:00 (before any history), 14:00 and 15:00 (bullish), 16:00 (expired), 17:00 (cleared after neutral)
    got = f["b_bias"].tolist()
    assert np.isnan(got[0]) and got[1:3] == [1.0, 1.0] and np.isnan(got[3]) and np.isnan(got[4])
    assert describe("b_bias")[0] == "bias"
