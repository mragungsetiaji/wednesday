import io
import pickle
import time
import zipfile

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams, build_detectors
from wednesday.engine import Runtime
from wednesday.feeds import SyntheticFeed
from wednesday.lab.dataset import FeatureParams, LADDER, candle_features, label_matrix, minute_dataset, unix
from wednesday.lab.model import ModelBundle, ModelFileError, load_bytes, read_manifest, safe_loads
from wednesday.lab.outcome import TradePlan, plan_levels, simulate
from wednesday.lab.service import Lab
from wednesday.lab.tags import TAGS, tag_of
from wednesday.lab.train import TrainParams, predict_blocks, train_bundle
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.structure import Context
from wednesday.timeframes import TIMEFRAMES_BY_NAME, resample_ohlcv

T0 = pd.Timestamp("2026-03-02 00:00")
M5 = TIMEFRAMES_BY_NAME["5M"]


@pytest.fixture(autouse=True)
def quick_models(monkeypatch):
    """Small models: the tests check the plumbing, not the fit."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    from wednesday.lab import train

    monkeypatch.setattr(train, "_classifier", lambda: HistGradientBoostingClassifier(max_iter=15))


def bars(rows, start=T0, freq="1min"):
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def synthetic(n=12_000, seed=7):
    return SyntheticFeed(seed=seed, history=n, end=pd.Timestamp("2026-03-20"))._bars


# ---- labels as data -------------------------------------------------------------

def test_label_matrix_is_one_zero_or_unknown():
    times = pd.date_range(T0, periods=6, freq="5min")
    u = [unix(t) for t in times]
    reviewed = [{"start": u[0], "end": u[3], "tags": ["ob_bull"]}]
    labels = [{"tag": "ob_bull", "value": 1, "start": u[1], "end": u[2]},
              {"tag": "ob_bull", "value": 0, "start": u[2], "end": u[2]},  # "not" loses to a yes on the same candle
              {"tag": "bsl", "value": 0, "start": u[5], "end": u[5]}]
    m = label_matrix(times, labels, reviewed, ["ob_bull", "bsl"])
    assert m["ob_bull"].tolist()[:4] == [0, 1, 1, 0]
    assert np.isnan(m["ob_bull"].iloc[4])  # outside the reviewed range: unknown, not negative
    assert np.isnan(m["bsl"].iloc[0]) and m["bsl"].iloc[5] == 0


def test_minute_dataset_uses_the_forming_candle_only():
    m1 = bars([(10, 11, 9, 10.5), (10.5, 13, 10, 12), (12, 12.5, 8, 9), (9, 10, 9, 9.5), (9.5, 9.6, 9.4, 9.5),
               (9.5, 20, 9, 19)])
    ds = minute_dataset(m1, {"5M": [{"tag": "ob_bull", "value": 1, "start": unix(T0), "end": unix(T0)}]},
                        {}, ["ob_bull"], (M5,))
    # Minute 2 knows the high of minutes 0-2 (13), not the 20 printed later in the next candle.
    assert ds["m5_high"].tolist()[:5] == [11, 13, 13, 13, 13]
    assert ds["m5_low"].tolist()[:5] == [9, 9, 8, 8, 8]
    assert ds["m5_open"].iloc[4] == 10 and ds["m5_close"].iloc[2] == 9
    assert ds["m5_high"].iloc[5] == 20 and ds["m5_open"].iloc[5] == 9.5  # a new candle starts
    assert ds["m5_ob_bull"].tolist()[:5] == [1] * 5 and np.isnan(ds["m5_ob_bull"].iloc[5])


def test_features_never_look_past_the_confirmation_candles():
    c = resample_ohlcv(synthetic(4000), M5)
    fp = FeatureParams(lookback=5, confirm=2)
    base = candle_features(c, M5, fp)
    i = 100
    changed = c.copy()
    changed.iloc[i + fp.confirm + 1:, :4] *= 1.05  # rewrite everything after candle i+confirm
    again = candle_features(changed, M5, fp)
    t = c.index[i]
    cols = [k for k in base.columns if not k.startswith("htf")]
    pd.testing.assert_series_equal(base.loc[t, cols], again.loc[t, cols])
    assert base.loc[t, "available_at"] == c.index[i + fp.confirm] + M5.delta
    assert c.index[-1] not in base.index  # the last candles have no confirmation yet


# ---- traded outcome ---------------------------------------------------------------

def test_plan_levels_cap_the_stop():
    assert plan_levels("bullish", 105, 100, 3.0) == (105, 102)
    assert plan_levels("bearish", 105, 100, 3.0) == (100, 103)
    assert plan_levels("bullish", 100, 100, 3.0) is None


@pytest.mark.parametrize("rows,expected", [
    ([(106, 107, 105.5, 106), (106, 106, 104.9, 105.5), (105.5, 111.5, 105, 111)], ("win", 2.0)),  # fill, then 2R
    ([(106, 107, 105.5, 106), (106, 106, 104.9, 105.5), (105.5, 106, 101, 101)], ("loss", -1.0)),
    ([(106, 107, 105.5, 106), (106, 106, 101, 104), (104, 120, 104, 119)], ("loss", -1.0)),  # stop on the fill bar
    ([(106, 107, 105.5, 106), (106, 108, 105.2, 107)], ("untouched", None)),
    ([(106, 107, 104.8, 105), (105, 106, 104, 105.5)], ("open", None)),
])
def test_simulate(rows, expected):
    m1 = bars(rows)
    assert simulate(m1, "bullish", 105, 103, T0, TradePlan(rr=2.0)) == expected


# ---- model files -----------------------------------------------------------------

class Boom:
    def __reduce__(self):
        import os
        return (os.system, ("echo pwned",))


def test_safe_unpickler_refuses_code():
    with pytest.raises(ModelFileError, match="posix.system|os.system|nt.system"):
        safe_loads(pickle.dumps({"models": {"x": Boom()}}))


def test_model_file_round_trip_and_tamper_check():
    from sklearn.ensemble import HistGradientBoostingClassifier

    X = pd.DataFrame({"a": np.linspace(0, 1, 40)})
    model = HistGradientBoostingClassifier(max_iter=5).fit(X, (X["a"] > 0.5).astype(int))
    bundle = ModelBundle({"id": "m1", "name": "test", "tags": {}, "params": {"lookback": 2, "confirm": 1}},
                         {"ob_bull": model}, None, ["a"])
    data = bundle.to_bytes()
    assert read_manifest(data)["sha256"] == bundle.manifest["sha256"]
    loaded = load_bytes(data)
    assert loaded.features == ["a"] and loaded.models["ob_bull"].predict(X).sum() == 20

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = zf.read("manifest.json")
    swapped = io.BytesIO()
    with zipfile.ZipFile(swapped, "w") as zf:
        zf.writestr("manifest.json", manifest)
        zf.writestr("model.pkl", pickle.dumps({"models": {"x": Boom()}}))
    with pytest.raises(ModelFileError, match="doesn't match"):
        load_bytes(swapped.getvalue())
    with pytest.raises(ModelFileError, match="not a zip"):
        read_manifest(b"hello")


# ---- training and prediction ---------------------------------------------------------

def detector_labels(m1, tf, until):
    c = resample_ohlcv(m1, tf)
    levels = [lv for d in build_detectors(("ob", "liquidity"), DetectorParams()) for lv in d.detect(Context(c))]
    labels = [{"tag": tag_of(lv), "value": 1, "start": unix(lv.time), "end": unix(lv.time), "top": lv.top,
               "bottom": lv.bottom} for lv in levels if lv.time < until]
    reviewed = [{"start": unix(c.index[0]), "end": unix(until), "tags": ["ob_bull", "ob_bear", "bsl", "ssl"]}]
    return labels, reviewed


def test_train_and_predict_on_detector_labels():
    m1 = synthetic(9000)
    tf = TIMEFRAMES_BY_NAME["15M"]
    labels, reviewed = detector_labels(m1, tf, m1.index[-2000])
    params = TrainParams(timeframes=["15M"], tags=["ob_bull", "ob_bear", "bsl", "ssl", "idm_bull"])
    bundle = train_bundle(m1, {"15M": labels}, {"15M": reviewed}, params, "XAU", DetectorParams())
    m = bundle.manifest
    assert m["tags"]["bsl"]["trained"] and m["tags"]["bsl"]["test_samples"] > 0
    assert m["tags"]["idm_bull"]["trained"] is False and "skipped" in m["tags"]["idm_bull"]
    assert m["outcome"]["samples"] > 0 and m["params"]["confirm"] == 3
    blocks = predict_blocks(load_bytes(bundle.to_bytes()), m1, tf, limit=150)
    assert blocks and {b["tag"] for b in blocks} <= set(TAGS)
    ob = next((b for b in blocks if b["tag"].startswith("ob_")), None)
    assert ob is None or ob["top"] >= ob["bottom"]


def test_nothing_to_train_says_why():
    m1 = synthetic(3000)
    with pytest.raises(ValueError, match="Nothing to train"):
        train_bundle(m1, {}, {}, TrainParams(timeframes=["15M"], tags=["bsl"]), "XAU", DetectorParams())


def test_train_params_validation():
    assert TrainParams(timeframes=["2H"]).validate()
    assert TrainParams(tags=["nope"]).validate()
    assert TrainParams.from_dict({"rr": 3, "junk": 1}).rr == 3
    assert not TrainParams().validate()


# ---- API ---------------------------------------------------------------------------------

@pytest.fixture
def lab_api(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'lab.db'}")
    cfg = ScanConfig(lookback=40, timeframes=(TIMEFRAMES_BY_NAME["15M"], TIMEFRAMES_BY_NAME["5M"]))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store,
                      lab=Lab(store, tmp_path / "models", cfg.params))
    runtime.engine.feed = SyntheticFeed(seed=3, history=9000)
    runtime.engine.buffer.feed = runtime.engine.feed
    runtime.engine.buffer.max_bars = 9000
    runtime.engine.step()
    return TestClient(create_app(runtime, ui_dir=tmp_path)), runtime


def wait_training(api):
    for _ in range(600):
        st = api.get("/api/lab").json()
        if not st["training"]["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("training never finished")


def test_lab_api_flow(lab_api):
    api, runtime = lab_api
    st = api.get("/api/lab").json()
    assert st["editable"] and st["available"] and st["history"]["bars"] == 9000 and not st["models"]

    win = api.get("/api/lab/candles", params={"tf": "15M", "limit": 400}).json()
    assert len(win["candles"]) == 400 and win["suggestions"] and win["is_latest"]
    older = api.get("/api/lab/candles", params={"tf": "15M", "limit": 50, "end": win["candles"][100]["time"]}).json()
    assert older["candles"][-1]["time"] == win["candles"][100]["time"]

    assert api.post("/api/lab/labels", json={"timeframe": "15M", "tag": "nope", "start": 1}).status_code == 422
    # Accept every detector suggestion of the first 300 candles and mark them reviewed.
    cut = win["candles"][300]["time"]
    for s in win["suggestions"]:
        if s["time_unix"] < cut and s["tag"] in ("bsl", "ssl", "ob_bull", "ob_bear"):
            api.post("/api/lab/labels", json={"timeframe": "15M", "tag": s["tag"], "start": s["time_unix"],
                                              "top": s["top"], "bottom": s["bottom"], "origin": "detector"})
    reviewed = api.post("/api/lab/reviewed", json={"timeframe": "15M", "start": win["candles"][0]["time"], "end": cut,
                                                   "tags": ["bsl", "ssl", "ob_bull", "ob_bear"]}).json()
    counts = api.get("/api/lab").json()["counts"]["15M"]
    assert counts["reviewed"] == 1 and counts["tags"]["bsl"]["yes"] > 0

    csv = api.get("/api/lab/dataset", params={"format": "csv", "days": 2})
    assert csv.status_code == 200 and "m15_bsl" in csv.text.splitlines()[0]
    parquet = api.get("/api/lab/dataset", params={"days": 2})
    assert parquet.content[:4] == b"PAR1"

    assert api.post("/api/lab/train", json={"timeframes": ["2H"]}).status_code == 422
    assert api.post("/api/lab/train", json={"timeframes": ["15M"], "tags": ["bsl", "ssl", "ob_bull", "ob_bear"],
                                            "name": "first"}).status_code == 202
    st = wait_training(api)
    assert st["training"]["error"] is None, st["training"]
    model = st["models"][0]
    assert model["name"] == "first" and st["active"] is None

    assert api.put("/api/lab/active", json={"id": model["id"]}).json()["active"] == model["id"]
    preds = api.get("/api/lab/predictions", params={"tf": "15M", "threshold": 0.3}).json()
    assert preds["model"]["id"] == model["id"]
    assert api.get("/api/lab/candles", params={"tf": "15M", "model": True}).status_code == 200

    block = preds["blocks"][0]
    review = api.post("/api/lab/reviews", json={**block, "verdict": "invalid"}).json()
    assert review["verdict"] == "invalid"
    labels = runtime.lab.labels(runtime.engine.symbol, "15M", block["time_unix"], block["time_unix"])
    assert any(lb["origin"] == "review" and lb["value"] == 0 for lb in labels)
    again = api.get("/api/lab/predictions", params={"tf": "15M", "threshold": 0.3}).json()
    assert next(b for b in again["blocks"] if b["id"] == block["id"])["verdict"] == "invalid"
    fb = api.get("/api/lab/feedback")
    assert fb.status_code == 200 and "human_reward" in fb.text

    # Export, delete, then import: the manifest first, the model only after confirming.
    data = api.get(f"/api/lab/models/{model['id']}/file").content
    assert api.delete(f"/api/lab/models/{model['id']}").json()["active"] is None
    staged = api.post("/api/lab/models/import", content=data).json()
    assert staged["manifest"]["name"] == "first" and not staged["exists"]
    assert api.get("/api/lab").json()["models"] == []  # nothing loaded yet
    done = api.post(f"/api/lab/models/import/{staged['token']}").json()
    assert done["imported"]["id"] == model["id"] and len(done["models"]) == 1
    assert api.post(f"/api/lab/models/import/{staged['token']}").status_code == 422  # used up
    assert api.post("/api/lab/models/import", content=b"not a zip").status_code == 422

    assert api.delete(f"/api/lab/reviewed/{reviewed['id']}").json()["deleted"]


def test_lab_without_database(tmp_path):
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), None)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    assert api.get("/api/lab").json()["editable"] is False
    assert api.get("/api/lab/predictions").json() == {"model": None, "blocks": []}
    assert api.post("/api/lab/labels", json={}).status_code == 409
