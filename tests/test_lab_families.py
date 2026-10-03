"""Optional feature families (issue #10): causal, recorded in the manifest, rebuilt for prediction."""

import pandas as pd
import pytest

from wednesday.detectors import DetectorParams
from wednesday.lab.dataset import FAMILIES, FeatureParams, candle_features
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
    assert set(FAMILIES) == {"quarters"}


@pytest.mark.parametrize("params", [{"lookback": 10, "confirm": 3}, {"lookback": 4, "confirm": 1, "families": None}])
def test_models_from_before_families_build_the_old_features(params):
    assert FeatureParams.of(params).families == ()
