import io
import json
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
from wednesday.lab import explain as explain_mod
from wednesday.lab import train as train_mod
from wednesday.lab.explain import describe
from wednesday.lab.service import Lab, feed_warnings
from wednesday.lab.tags import TAGS, tag_of
from wednesday.lab.train import TrainParams, predict_blocks, train_bundle
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store, lab_labels_table
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


@pytest.mark.parametrize("folds", [1, 2, 4, 6])
def test_walk_forward_folds_never_test_before_the_gap(folds):
    rng = np.random.default_rng(1)
    # Uneven times with ties, like samples pooled over timeframes.
    when = np.sort(pd.Timestamp("2026-01-01").to_datetime64()
                   + (rng.integers(0, 60 * 24 * 30, 900) * 60).astype("timedelta64[s]"))
    rng.shuffle(when)
    gap = pd.Timedelta(minutes=45)
    splits = train_mod.walk_forward_splits(when, folds, 0.2, gap)
    assert len(splits) == folds
    prev_train = -1
    for fit, tune, test in splits:
        train = np.concatenate([fit, tune])
        end = when[train].max()
        assert len(test) and (when[test] >= end + gap.to_timedelta64()).all()
        assert when[fit].max() <= when[tune].min()  # the cut is tuned on the latest slice of training
        assert len(train) > prev_train  # expanding window
        prev_train = len(train)
    if folds > 1:  # the test windows follow each other without overlap
        spans = [(when[t].min(), when[t].max()) for _, _, t in splits]
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_training_scores_every_fold():
    m1 = synthetic(9000)
    tf = TIMEFRAMES_BY_NAME["15M"]
    labels, reviewed = detector_labels(m1, tf, m1.index[-2000])
    params = TrainParams(timeframes=["15M"], tags=["bsl", "ob_bull", "ob_bear"], folds=3)
    m = train_bundle(m1, {"15M": labels}, {"15M": reviewed}, params, "XAU", DetectorParams()).manifest
    bsl = m["tags"]["bsl"]
    assert len(bsl["folds"]) == 3 and bsl["folds_asked"] == 3
    assert bsl["precision"] == pytest.approx(np.mean([f["precision"] for f in bsl["folds"]]))
    assert bsl["threshold"] == bsl["folds"][-1]["threshold"] and "auc" in bsl["spread"]
    assert all("avg_r_all" in f for f in m["outcome"]["folds"])
    assert m["params"]["folds"] == 3 and m["params"]["gap_minutes"] == 45
    one = train_bundle(m1, {"15M": labels}, {"15M": reviewed}, TrainParams(timeframes=["15M"], tags=["bsl"], folds=1),
                       "XAU", DetectorParams()).manifest
    assert "folds" not in one["tags"]["bsl"] and one["tags"]["bsl"]["test_samples"] > 0  # one split, as before


def test_nothing_to_train_says_why():
    m1 = synthetic(3000)
    with pytest.raises(ValueError, match="Nothing to train"):
        train_bundle(m1, {}, {}, TrainParams(timeframes=["15M"], tags=["bsl"]), "XAU", DetectorParams())


def test_train_params_validation():
    assert TrainParams(timeframes=["2H"]).validate()
    assert TrainParams(tags=["nope"]).validate()
    assert TrainParams(folds=9).validate() and TrainParams(folds=0).validate()
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
    assert st["runs"][0]["status"] == "done" and st["runs"][0]["model_id"] == model["id"]
    assert api.delete("/api/lab/train").status_code == 409  # nothing to stop

    assert api.get("/api/lab/queue", params={"tf": "15M"}).status_code == 409  # no active model yet
    assert api.put("/api/lab/active", json={"id": model["id"]}).json()["active"] == model["id"]

    # Review queue: least sure first, labelled (candle, tag) pairs left out, and they drop out once tagged.
    q = api.get("/api/lab/queue", params={"tf": "15M", "limit": 20}).json()
    assert q["model_id"] == model["id"] and q["total"] > 20 and len(q["items"]) == 20
    dist = [i["distance"] for i in q["items"]]
    assert dist == sorted(dist) and all(abs(i["prob"] - i["cut"]) == pytest.approx(i["distance"]) for i in q["items"])
    done = {(lb["tag"], lb["start"]) for lb in runtime.lab.labels(runtime.engine.symbol, "15M")}
    assert not [i for i in q["items"] if (i["tag"], i["time_unix"]) in done]
    top = q["items"][0]
    api.post("/api/lab/labels", json={"timeframe": "15M", "tag": top["tag"], "start": top["time_unix"], "value": 0})
    q2 = api.get("/api/lab/queue", params={"tf": "15M", "limit": 20}).json()
    assert top["id"] not in {i["id"] for i in q2["items"]} and q2["total"] == q["total"] - 1
    only = api.get("/api/lab/queue", params={"tf": "15M", "tag": "bsl", "limit": 50}).json()
    assert only["items"] and {i["tag"] for i in only["items"]} == {"bsl"}
    span = (reviewed["start"], reviewed["end"])
    inside = api.get("/api/lab/queue", params={"tf": "15M", "scope": "inside", "limit": 1000}).json()
    outside = api.get("/api/lab/queue", params={"tf": "15M", "scope": "outside", "limit": 1000}).json()
    assert inside["total"] + outside["total"] == q2["total"]
    assert all(span[0] <= i["time_unix"] <= span[1] for i in inside["items"])
    assert not [i for i in outside["items"] if span[0] <= i["time_unix"] <= span[1]]
    assert api.get("/api/lab/queue", params={"tf": "15M", "scope": "nope"}).status_code == 422
    assert api.get("/api/lab/queue", params={"tf": "5M"}).json()["note"]  # not trained on 5M
    card = api.get("/api/lab/scorecard").json()["scorecard"]
    assert card["model_id"] == model["id"] and card["since_unix"] and card["reviews"]["n"] == 0
    assert card["warnings"] == [] and card["inputs"] is not None
    assert api.post("/api/lab/compare", json={"ids": [model["id"]]}).status_code == 422
    batch = api.post("/api/lab/batch", json={"add_labels": [{"timeframe": "15M", "tag": "bsl", "start": 1}]}).json()
    assert api.post("/api/lab/batch", json={"delete_labels": [batch["labels"][0]["id"]]}).json()["deleted_labels"]
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
    assert staged["manifest"]["feed"]["clock"] and staged["warnings"] == []  # same feed it was trained on
    assert api.get("/api/lab").json()["models"] == []  # nothing loaded yet
    assert staged["signature"]["state"] == "unsigned"
    refused = api.post(f"/api/lab/models/import/{staged['token']}", json={})
    assert refused.status_code == 422 and "trust" in refused.json()["detail"]  # needs "I trust this file"
    done = api.post(f"/api/lab/models/import/{staged['token']}", json={"trust": True}).json()
    assert done["imported"]["id"] == model["id"] and len(done["models"]) == 1
    assert done["models"][0]["signature"]["state"] == "unsigned"
    assert api.post(f"/api/lab/models/import/{staged['token']}", json={"trust": True}).status_code == 422  # used up
    assert api.post("/api/lab/models/import", content=b"not a zip").status_code == 422

    assert api.delete(f"/api/lab/reviewed/{reviewed['id']}").json()["deleted"]


def test_feature_names_read_plainly():
    assert describe("body_0") == ("candle", "candle body")
    assert describe("close_-3") == ("before", "3 candles before: close vs candle")
    assert describe("uwick_2") == ("confirm", "confirming candle 2: upper wick")
    assert describe("htf1_close")[0] == "htf" and describe("hour_sin")[0] == "time"
    assert describe("zone_risk") == ("zone", "stop distance")


def test_importance_is_computed_on_held_out_data(monkeypatch):
    from wednesday.lab import train

    seen = []
    real = train.importance
    monkeypatch.setattr(train, "importance", lambda model, X, y: seen.append(X.index) or real(model, X, y))
    m1 = synthetic(9000)
    tf = TIMEFRAMES_BY_NAME["15M"]
    labels, reviewed = detector_labels(m1, tf, m1.index[-2000])
    params = TrainParams(timeframes=["15M"], tags=["bsl", "ssl"])
    bundle = train_bundle(m1, {"15M": labels}, {"15M": reviewed}, params, "XAU", DetectorParams())
    imp = bundle.manifest["tags"]["bsl"]["importance"]
    assert imp["metric"] == "auc" and imp["families"] and len(imp["features"]) <= 15
    assert {f["id"] for f in imp["families"]} <= {"candle", "before", "confirm", "window", "volatility", "time",
                                                   "timeframe", "htf"}
    assert all(f["label"] for f in imp["features"])
    test_from = pd.Timestamp(bundle.manifest["tags"]["bsl"]["test_from"])
    # Every sample it was scored on is from the held-out part (by when it became known).
    frames = train.frames_for(m1, tf, params.features)
    assert all((frames.feats.loc[idx, "available_at"] >= test_from).all() for idx in seen)


def slow_train(monkeypatch, steps=200):
    """train_bundle that only reports progress, so a test can stop it midway."""
    from wednesday.lab import service

    def fake(m1, labels, reviewed, params, symbol, detector, progress):
        for i in range(steps):
            progress(f"step {i}")
            time.sleep(0.01)
        raise AssertionError("never cancelled")

    monkeypatch.setattr(service, "train_bundle", fake)


def test_cancel_training_saves_nothing(tmp_path, monkeypatch):
    slow_train(monkeypatch)
    store = Store(f"sqlite:///{tmp_path / 'lab.db'}")
    lab = Lab(store, tmp_path / "models", DetectorParams())
    assert not lab.cancel_training()
    lab.start_training(synthetic(3000), "XAU", TrainParams())
    with pytest.raises(RuntimeError):
        lab.start_training(synthetic(3000), "XAU", TrainParams())
    assert lab.cancel_training()
    for _ in range(200):
        if not lab.training["running"]:
            break
        time.sleep(0.02)
    assert lab.training["cancelled"] and lab.training["error"] is None
    assert not (tmp_path / "models").exists() or not any((tmp_path / "models").iterdir())
    run = lab.runs()[0]
    assert run["status"] == "cancelled" and run["finished_at"] and run["params"]["timeframes"] == ["5M", "15M"]


def test_training_runs_survive_a_restart(tmp_path, monkeypatch):
    slow_train(monkeypatch, steps=10_000)
    store = Store(f"sqlite:///{tmp_path / 'lab.db'}")
    lab = Lab(store, tmp_path / "models", DetectorParams())
    lab.start_training(synthetic(3000), "XAU", TrainParams())
    # A new Lab on the same database is what a restart looks like; the old thread is still going.
    runs = Lab(store, tmp_path / "models", DetectorParams()).runs()
    assert runs[0]["status"] == "interrupted" and "stopped" in runs[0]["error"]
    lab.cancel_training()
    for i in range(25):
        store.lab_run_put({"id": f"r{i}", "symbol": "XAU", "started_at": f"2026-01-{i + 1:02d}", "finished_at": None,
                           "status": "done", "error": None, "model_id": None, "params": {}})
    assert len(store.lab_runs(limit=100)) == 20


def test_feed_warnings():
    m = {"symbol": "XAUUSD", "feed": {"source": "mt5", "clock": "Etc/GMT-3"}}
    assert feed_warnings(m, "XAUUSD", "Etc/GMT-3") == []
    both = feed_warnings(m, "GC=F", "UTC")
    assert len(both) == 2 and "GC=F" in both[0] and "UTC" in both[1]
    assert "doesn't say" in feed_warnings({"symbol": "XAUUSD"}, "XAUUSD", "UTC")[0]


def test_labels_export_wipe_import_gives_the_same_samples(lab_api, tmp_path):
    api, runtime = lab_api
    sym = runtime.engine.symbol
    win = api.get("/api/lab/candles", params={"tf": "15M", "limit": 300}).json()
    for s in win["suggestions"]:
        if s["tag"] in ("bsl", "ssl"):
            api.post("/api/lab/labels", json={"timeframe": "15M", "tag": s["tag"], "start": s["time_unix"],
                                              "top": s["top"], "bottom": s["bottom"], "origin": "detector"})
    api.post("/api/lab/reviewed", json={"timeframe": "15M", "start": win["candles"][0]["time"],
                                        "end": win["candles"][-1]["time"], "tags": ["bsl", "ssl"]})
    other = runtime.lab.add_label("EURUSD", {"timeframe": "15M", "tag": "bsl", "start": 1, "value": 1})

    exported = api.get("/api/lab/labels/export")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    doc = exported.json()
    assert doc["symbol"] == sym and doc["labels"] and len(doc["reviewed"]) == 1

    def samples(lab):
        m1 = runtime.engine.snapshot()[2]
        fr =train_mod.frames_for(m1, TIMEFRAMES_BY_NAME["15M"], TrainParams().features)
        X, Y = train_mod.tag_samples([fr], lab.by_tf(lab.labels(sym)), lab.by_tf(lab.reviewed(sym)), ["bsl", "ssl"])
        return Y.sort_index()

    before = samples(runtime.lab)
    # Wipe this symbol's labels; the other symbol's label shares the database.
    for r in runtime.lab.labels(sym):
        runtime.lab.delete("labels", r["id"])
    for r in runtime.lab.reviewed(sym):
        runtime.lab.delete("reviewed", r["id"])
    assert samples(runtime.lab).empty

    staged = api.post("/api/lab/labels/import", content=exported.content).json()
    assert staged["matches"] and staged["labels"]["new"] == len(doc["labels"]) and staged["reviewed"]["new"] == 1
    done = api.post(f"/api/lab/labels/import/{staged['token']}", json={}).json()
    assert done["imported"]["labels"]["new"] == len(doc["labels"])
    pd.testing.assert_frame_equal(samples(runtime.lab), before)
    assert runtime.lab.labels("EURUSD") == [other]

    # Again: same ids update, nothing doubles.
    again = api.post("/api/lab/labels/import", content=exported.content).json()
    assert again["labels"] == {"new": 0, "updated": len(doc["labels"]), "skipped": 0}

    # A file of another symbol needs map_symbol, and never touches that symbol's labels.
    foreign = json.dumps({**doc, "symbol": "EURUSD", "labels": [{**other, "start": 5, "end": 5}]}).encode()
    staged = api.post("/api/lab/labels/import", content=foreign).json()
    assert not staged["matches"]
    assert api.post(f"/api/lab/labels/import/{staged['token']}", json={}).status_code == 422
    assert api.post(f"/api/lab/labels/import/{staged['token']}", json={"map_symbol": True}).status_code == 200
    assert runtime.lab.labels("EURUSD") == [other]
    assert any(r["start"] == 5 and r["id"] != other["id"] for r in runtime.lab.labels(sym))

    assert api.post("/api/lab/labels/import", content=b"nope").status_code == 422
    assert api.post("/api/lab/labels/import", content=b'{"format": "wednesday-labels", "labels": [{"tag": "x"}]}'
                    ).status_code == 422


def test_old_database_gets_the_new_label_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE lab_labels (id VARCHAR(36) PRIMARY KEY, symbol VARCHAR(64) NOT NULL, timeframe VARCHAR(8) '
                'NOT NULL, tag VARCHAR(32) NOT NULL, value INTEGER NOT NULL, start BIGINT NOT NULL, "end" BIGINT NOT NULL, '
                'top FLOAT, bottom FLOAT, origin VARCHAR(16) NOT NULL, created_at VARCHAR(40) NOT NULL)')
    con.execute("INSERT INTO lab_labels VALUES ('a', 'XAU', '5M', 'bsl', 1, 10, 10, NULL, NULL, 'manual', '2026-01-01')")
    con.commit()
    con.close()
    store = Store(f"sqlite:///{path}")
    row = store.lab_rows(lab_labels_table, "XAU")[0]
    assert row["id"] == "a" and row["updated_at"] is None and row["changed_by"] is None
    lab = Lab(store, tmp_path / "models", DetectorParams())
    new = lab.add_label("XAU", {"timeframe": "5M", "tag": "ssl", "start": 20})
    assert new["updated_at"] and new["changed_by"] == "manual"


def test_batch_undo_restores_exactly(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'lab.db'}")
    lab = Lab(store, tmp_path / "models", DetectorParams())
    a = lab.add_label("XAU", {"timeframe": "5M", "tag": "ob_bull", "start": 100, "end": 200, "top": 5.5, "bottom": 5.0},
                      origin="detector")
    b = lab.add_label("XAU", {"timeframe": "5M", "tag": "bsl", "start": 300, "value": 0})
    rv = lab.add_reviewed("XAU", {"timeframe": "5M", "start": 0, "end": 400, "tags": ["ob_bull", "bsl"]})
    other = lab.add_label("EUR", {"timeframe": "5M", "tag": "bsl", "start": 300})
    before = (lab.labels("XAU"), lab.reviewed("XAU"))

    # A bulk clear of the selection, then its undo: the deleted rows go back as they were.
    done = lab.batch("XAU", {"delete_labels": [a["id"], b["id"], other["id"]], "delete_reviewed": [rv["id"]]})
    assert {r["id"] for r in done["deleted_labels"]} == {a["id"], b["id"]}  # never another symbol's
    assert lab.labels("XAU") == [] and lab.labels("EUR") == [other]
    lab.batch("XAU", {"add_labels": done["deleted_labels"], "add_reviewed": done["deleted_reviewed"]})
    assert (lab.labels("XAU"), lab.reviewed("XAU")) == before

    # Accepting suggestions in bulk, then undoing it by deleting what was written.
    added = lab.batch("XAU", {"add_labels": [{"timeframe": "5M", "tag": "ssl", "start": t, "origin": "detector"}
                                             for t in (500, 600)]})
    assert [r["origin"] for r in added["labels"]] == ["detector", "detector"]
    lab.batch("XAU", {"delete_labels": [r["id"] for r in added["labels"]]})
    assert (lab.labels("XAU"), lab.reviewed("XAU")) == before
    with pytest.raises(ValueError, match="another symbol"):
        lab.batch("XAU", {"add_labels": [{**other, "symbol": "XAU"}]})


def test_blocks_say_why(tmp_path):
    m1 = synthetic(9000)
    tf = TIMEFRAMES_BY_NAME["15M"]
    labels, reviewed = detector_labels(m1, tf, m1.index[-2000])
    bundle = train_bundle(m1, {"15M": labels}, {"15M": reviewed}, TrainParams(timeframes=["15M"], tags=["bsl", "ssl"]),
                          "XAU", DetectorParams())
    assert bundle.medians["tags"] and bundle.manifest["inputs"]["15M"]["atr_pct"][0] > 0
    loaded = load_bytes(bundle.to_bytes())
    blocks = predict_blocks(loaded, m1, tf, limit=150)
    assert blocks and all(1 <= len(b["why"]) <= 3 for b in blocks)
    assert {w["title"] for b in blocks for w in b["why"]} <= set(explain_mod.FAMILIES.values())
    loaded.medians = {}  # a file from before 0.1.7
    assert all(b["why"] == [] for b in predict_blocks(loaded, m1, tf, limit=150))


def test_score_models_on_the_same_window():
    m1 = synthetic(12_000)
    tf = TIMEFRAMES_BY_NAME["15M"]
    labels, reviewed = detector_labels(m1, tf, m1.index[-1])
    early, later = m1.iloc[:-3000], m1.iloc[:-2500]
    tags = ["bsl", "ssl", "ob_bull", "ob_bear"]
    a = train_bundle(early, {"15M": labels}, {"15M": reviewed}, TrainParams(timeframes=["15M"], tags=tags, name="a"),
                     "XAU", DetectorParams())
    b = train_bundle(later, {"15M": labels}, {"15M": reviewed},
                     TrainParams(timeframes=["15M", "5M"], tags=tags, lookback=6, name="b"), "XAU", DetectorParams())
    out = train_mod.score_window([a, b], m1, {"15M": labels}, {"15M": reviewed}, DetectorParams())
    assert out["timeframes"] == ["15M"] and out["from"] == b.manifest["data"]["last"]
    for res in out["models"].values():
        assert res["tags"]["bsl"]["samples"] > 0
    # Only candles known after the newest model's data: never what either model trained on.
    fr = train_mod.frames_for(m1, tf, TrainParams().features)
    X, _ = train_mod.tag_samples([fr], {"15M": labels}, {"15M": reviewed}, ["bsl"])
    n_after = int((X["available_at"] > pd.Timestamp(out["from"])).sum())
    assert out["models"][a.id]["tags"]["bsl"]["samples"] <= n_after

    with pytest.raises(ValueError, match="Nothing to score"):
        train_mod.score_window([a, b], later, {"15M": labels}, {"15M": reviewed}, DetectorParams())
    c = train_bundle(early, {"5M": detector_labels(early, M5, early.index[-1])[0]},
                     {"5M": detector_labels(early, M5, early.index[-1])[1]}, TrainParams(timeframes=["5M"], tags=["bsl"]),
                     "XAU", DetectorParams())
    with pytest.raises(ValueError, match="share no timeframe"):
        train_mod.score_window([a, c], m1, {}, {}, DetectorParams())


def test_drift_scorecard():
    from wednesday.lab import drift

    manifest = {"tags": {"bsl": {"precision": 0.8}}}
    few = [{"tag": "bsl", "verdict": "invalid"}] * 5
    assert not drift.review_score(manifest, few)["low"]  # no alarm on a handful
    many = [{"tag": "bsl", "verdict": "invalid"}] * 20 + [{"tag": "bsl", "verdict": "valid"}] * 10
    score = drift.review_score(manifest, many)
    assert score["low"] and score["n"] == 30 and score["expected"] == pytest.approx(0.8)
    fine = [{"tag": "bsl", "verdict": "valid"}] * 24 + [{"tag": "bsl", "verdict": "invalid"}] * 6
    assert not drift.review_score(manifest, fine)["low"]

    m1 = synthetic(9000)
    tf = TIMEFRAMES_BY_NAME["15M"]
    labels, reviewed = detector_labels(m1, tf, m1.index[-2000])
    bundle = train_bundle(m1.iloc[:-2000], {"15M": labels}, {"15M": reviewed},
                          TrainParams(timeframes=["15M"], tags=["ob_bull", "ob_bear", "bsl"]), "XAU", DetectorParams())
    since = unix(m1.index[-2000])
    market = drift.market_score(bundle, m1, since)
    assert market["calls"] >= market["finished"] >= market["wins"] >= 0
    shifts = drift.input_shift(bundle, m1)
    assert shifts and not any(s["shifted"] for s in shifts)  # same synthetic market
    wild = m1.copy()
    mid = wild["close"].mean()
    for col in ("open", "high", "low", "close"):
        wild[col] = mid + (wild[col] - mid) * 4  # four times the moves, same price level
    assert any(s["shifted"] and s["input"] == "atr_pct" for s in drift.input_shift(bundle, wild))
    card = drift.scorecard(bundle, [], wild, "2026-01-01T00:00:00", since)
    assert any("ATR is" in w for w in card["warnings"])
    bundle.manifest.pop("inputs")  # a file from before 0.1.7: reviews and market only
    assert drift.input_shift(bundle, m1) is None


def small_model_file(model_id="m1") -> bytes:
    from sklearn.ensemble import HistGradientBoostingClassifier

    X = pd.DataFrame({"a": np.linspace(0, 1, 40)})
    model = HistGradientBoostingClassifier(max_iter=5).fit(X, (X["a"] > 0.5).astype(int))
    return ModelBundle({"id": model_id, "name": "test", "tags": {}, "params": {"lookback": 2, "confirm": 1}},
                       {"ob_bull": model}, None, ["a"]).to_bytes()


def rezip(data: bytes, name: str, change) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    files[name] = change(files[name])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for n, content in files.items():
            zf.writestr(n, content)
    return out.getvalue()


def test_sign_and_verify_model_files(tmp_path):
    from wednesday.lab import signing

    public = signing.new_key(tmp_path / "me.pem")
    with pytest.raises(signing.SigningError, match="already exists"):
        signing.new_key(tmp_path / "me.pem")
    assert (tmp_path / "me.pem").stat().st_mode & 0o077 == 0  # owner only
    key = signing.load_private_key(tmp_path / "me.pem")
    data = small_model_file()
    signed = signing.sign(data, key)

    assert signing.verify(data)["state"] == "unsigned"
    unknown = signing.verify(signed)
    assert unknown["state"] == "unknown" and unknown["key_id"] == signing.key_id(signing.parse_public_key(public))
    mine = signing.trusted_keys([{"name": "Me", "public_key": public}])
    assert signing.verify(signed, mine) == {"state": "trusted", "key_id": unknown["key_id"], "signer": "Me", "reason": None}
    assert load_bytes(signed).id == "m1"  # a signed file still loads the same way

    # One byte changed in the manifest or the pickle, and it no longer verifies.
    manifest_edit = rezip(signed, "manifest.json", lambda b: b.replace(b'"test"', b'"tesT"'))
    assert signing.verify(manifest_edit, mine)["state"] == "invalid"
    pickle_edit = rezip(signed, "model.pkl", lambda b: b[:-1] + bytes([b[-1] ^ 1]))
    assert signing.verify(pickle_edit, mine)["state"] == "invalid"
    # A new pickle with a matching hash in a re-signed manifest needs the private key: re-signing with
    # another key makes it "unknown", never "trusted".
    signing.new_key(tmp_path / "other.pem")
    other = signing.load_private_key(tmp_path / "other.pem")
    assert signing.verify(signing.sign(data, other), mine)["state"] == "unknown"
    with pytest.raises(signing.SigningError):
        signing.parse_public_key("not base64!")


def test_only_signed_models_load(lab_api, tmp_path):
    from wednesday.lab import signing

    api, runtime = lab_api
    public = signing.new_key(tmp_path / "me.pem")
    key = signing.load_private_key(tmp_path / "me.pem")
    unsigned = small_model_file("u1")
    signed = signing.sign(small_model_file("s1"), key)

    st = api.get("/api/lab/trust").json()
    assert st["only_signed"] is False and st["keys"][0]["name"] == "momentum.id" and st["keys"][0]["builtin"]
    assert api.put("/api/lab/trust", json={"keys": [{"name": "Me", "public_key": "nope"}]}).status_code == 422
    st = api.put("/api/lab/trust", json={"keys": [*st["keys"], {"name": "Me", "public_key": public}],
                                         "only_signed": True}).json()
    assert [k["name"] for k in st["keys"]] == ["momentum.id", "Me"] and st["only_signed"]

    staged = api.post("/api/lab/models/import", content=signed).json()
    assert staged["signature"]["state"] == "trusted" and staged["signature"]["signer"] == "Me"
    assert api.post(f"/api/lab/models/import/{staged['token']}", json={}).status_code == 200  # no checkbox needed
    staged = api.post("/api/lab/models/import", content=unsigned).json()
    blocked = api.post(f"/api/lab/models/import/{staged['token']}", json={"trust": True})
    assert blocked.status_code == 422 and "Only models signed" in blocked.json()["detail"]  # enforced by the API

    # The file on disk is checked again when it's set active.
    path = runtime.lab.models_dir / "s1.zip"
    assert api.put("/api/lab/active", json={"id": "s1"}).status_code == 200
    path.write_bytes(rezip(path.read_bytes(), "manifest.json", lambda b: b.replace(b'"test"', b'"tesT"')))
    assert api.put("/api/lab/active", json={"id": None}).status_code == 200
    refused = api.put("/api/lab/active", json={"id": "s1"})
    assert refused.status_code == 422 and "signature" in refused.json()["detail"]
    assert next(m for m in api.get("/api/lab").json()["models"] if m["id"] == "s1")["signature"]["state"] == "invalid"


def test_signing_cli(tmp_path, capsys):
    from wednesday import cli
    from wednesday.lab import signing

    key = tmp_path / "k.pem"
    cli.run(cli.parse_args(["--new-signing-key", str(key), "--db", "none"]))
    public = capsys.readouterr().out.split("Public key")[1].split(": ")[1].strip()
    model = tmp_path / "m.zip"
    model.write_bytes(small_model_file())
    cli.run(cli.parse_args(["--sign-model", str(model), "--key", str(key), "--db", "none"]))
    assert signing.verify(model.read_bytes(), signing.trusted_keys([{"name": "k", "public_key": public}]))["state"] == "trusted"


def test_lab_without_database(tmp_path):
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), None)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    assert api.get("/api/lab").json()["editable"] is False
    assert api.get("/api/lab/predictions").json() == {"model": None, "blocks": []}
    assert api.post("/api/lab/labels", json={}).status_code == 409
