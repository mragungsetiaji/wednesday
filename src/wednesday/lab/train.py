"""Training and prediction.

Two kinds of model go into one file:

* **Tag models**, one per tag: "is this candle an order block / liquidity /
  inducement?" Trained on your labels only (1 = tagged, 0 = reviewed and
  untagged or marked "not"), pooled over the chosen timeframes.
* **Outcome model**: "if this order block is traded with the limit plan, does
  it reach ``rr`` R before the stop?" The market gives this answer, so besides
  your order block labels it also learns from every order block the detector
  found in the history.

Every split is by time, by the moment a sample became known: walk-forward
folds (or one split) score the model on data after what it trained on, then
the model is refit on all. See :func:`walk_forward_splits`.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..detectors.base import DetectorParams
from ..detectors.orderblock import OrderBlockDetector
from ..structure import Context
from ..timeframes import TIMEFRAMES_BY_NAME, Timeframe
from .dataset import FeatureParams, _atr, feature_names, label_matrix, timeframe_features, ts, unix
from .explain import contributions, importance, medians
from .model import ModelBundle
from .outcome import TradePlan, plan_levels, simulate
from .tags import OB_TAGS, TAGS

MIN_CLASS = 5  # samples of each class needed to train a tag


class TrainingCancelled(Exception):
    """Raised from the progress callback when the trader stops a run."""


ZONE_FEATURES = ["zone_dir", "zone_body", "zone_risk", "zone_capped", "zone_entry"]


@dataclass
class TrainParams:
    timeframes: list[str] = field(default_factory=lambda: ["5M", "15M"])
    tags: list[str] = field(default_factory=lambda: list(TAGS))
    lookback: int = 10
    confirm: int = 3
    rr: float = 2.0
    horizon_hours: int = 72
    outcome_from_detector: bool = True
    test_fraction: float = 0.2
    folds: int = 4  # walk-forward folds; 1 = a single time split
    name: str = ""
    author: str = ""
    note: str = ""

    def validate(self) -> list[str]:
        errors = []
        bad = [t for t in self.timeframes if t not in TIMEFRAMES_BY_NAME]
        if bad or not self.timeframes:
            errors.append(f"Pick timeframes from {', '.join(TIMEFRAMES_BY_NAME)}")
        if not self.tags or any(t not in TAGS for t in self.tags):
            errors.append(f"Pick tags from {', '.join(TAGS)}")
        if not 2 <= self.lookback <= 50:
            errors.append("Lookback must be 2 to 50 candles")
        if not 1 <= self.confirm <= 20:
            errors.append("Confirmation must be 1 to 20 candles")
        if not 0.5 <= self.rr <= 10:
            errors.append("Target must be 0.5 to 10 R")
        if not 1 <= self.horizon_hours <= 24 * 30:
            errors.append("Horizon must be 1 hour to 30 days")
        if not 0.05 <= self.test_fraction <= 0.5:
            errors.append("Test share must be 5% to 50%")
        if not 1 <= self.folds <= 8:
            errors.append("Folds must be 1 to 8")
        return errors

    @classmethod
    def from_dict(cls, d: dict | None) -> TrainParams:
        base = asdict(cls())
        d = {k: v for k, v in (d or {}).items() if k in base}
        return cls(**{**base, **d})

    @property
    def features(self) -> FeatureParams:
        return FeatureParams(lookback=self.lookback, confirm=self.confirm)

    @property
    def gap(self) -> pd.Timedelta:
        """Purge between a fold's training and test parts: ``confirm`` candles of the largest
        timeframe, the most a sample's window reaches past the moment it is known."""
        return self.confirm * max(TIMEFRAMES_BY_NAME[t].delta for t in self.timeframes)


def _classifier():
    from sklearn.ensemble import HistGradientBoostingClassifier

    # No class weights: they make scikit-learn's binning very slow, and unweighted
    # probabilities read as real frequencies (the chart's threshold does the rest).
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.07, max_leaf_nodes=15,
                                          l2_regularization=1.0, random_state=0)


def best_threshold(y: np.ndarray, p: np.ndarray) -> float:
    """Probability cut with the best F1 (0.5 when there is nothing to tune on)."""
    if len(np.unique(y)) < 2:
        return 0.5
    best, cut = -1.0, 0.5
    for t in np.unique(np.round(p, 3)):
        pred = p >= t
        tp = float((pred & (y == 1)).sum())
        f1 = 2 * tp / (pred.sum() + y.sum()) if pred.sum() + y.sum() else 0.0
        if f1 > best:
            best, cut = f1, float(t)
    return min(max(cut, 0.05), 0.95)


def walk_forward_splits(when: np.ndarray, folds: int, test_fraction: float,
                        gap: pd.Timedelta = pd.Timedelta(0)) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """(fit, tune, test) index arrays, by the moment each sample became known.

    One fold is a single split: the latest ``test_fraction`` is tested. With more, the samples
    are cut into ``folds + 1`` equal slices in time and fold ``j`` tests slice ``j`` after
    training on everything before it (an expanding window). Each training part keeps its latest
    ``test_fraction`` to tune the probability cut. Test samples known less than ``gap`` after
    the training part's last one are dropped (a purge), so overlapping candle windows can't leak.
    """
    n = len(when)
    order = np.argsort(when, kind="stable")
    t = np.asarray(when)[order]
    if folds <= 1:
        bounds, ends = [int(round(n * (1 - test_fraction)))], [n]
    else:
        size = n / (folds + 1)
        bounds = [int(round(size * j)) for j in range(1, folds + 1)]
        ends = bounds[1:] + [n]
    purge = np.timedelta64(int(gap.total_seconds()), "s")
    out = []
    for b, e in zip(bounds, ends):
        if b <= 0 or e <= b:
            continue
        cut_tune = int(round(b * (1 - test_fraction)))
        test = order[b:e][t[b:e] >= t[b - 1] + purge]
        out.append((order[:cut_tune], order[cut_tune:b], test))
    return out


FOLD_KEYS = ("precision", "recall", "auc", "avg_precision", "base_rate", "avg_r_all", "avg_r_picked")
SPREAD_KEYS = ("precision", "recall", "auc", "avg_r_all", "avg_r_picked")


def _fold(X: pd.DataFrame, y: np.ndarray, when: np.ndarray, fit, tune, te, r: np.ndarray | None):
    """Fit on one fold's training part, tune the cut, score its test part. None when it can't."""
    from sklearn.metrics import average_precision_score, precision_score, recall_score, roc_auc_score

    if len(np.unique(y[fit])) < 2 or not len(te):
        return None
    model = _classifier().fit(X.iloc[fit], y[fit])
    threshold = best_threshold(y[tune], model.predict_proba(X.iloc[tune])[:, 1]) if len(tune) else 0.5
    p = model.predict_proba(X.iloc[te])[:, 1]
    pred = p >= threshold
    s = {"threshold": threshold, "test_samples": int(len(te)), "test_positives": int(y[te].sum()),
         "test_from": pd.Timestamp(when[te].min()).isoformat(), "test_to": pd.Timestamp(when[te].max()).isoformat(),
         "train_samples": int(len(fit) + len(tune)),
         "precision": float(precision_score(y[te], pred, zero_division=0)),
         "recall": float(recall_score(y[te], pred, zero_division=0)), "base_rate": float(y[te].mean())}
    if len(np.unique(y[te])) == 2:
        s["auc"] = float(roc_auc_score(y[te], p))
        s["avg_precision"] = float(average_precision_score(y[te], p))
    if r is not None:
        s["avg_r_all"] = float(r[te].mean())
        s["picked"] = int(pred.sum())
        s["avg_r_picked"] = float(r[te][pred].mean()) if pred.any() else None
    return s, model


def fit_eval(X: pd.DataFrame, y: np.ndarray, when: np.ndarray, test_fraction: float,
             r: np.ndarray | None = None, folds: int = 1, gap: pd.Timedelta = pd.Timedelta(0)) -> tuple[object | None, dict]:
    """Score by time (one split, or walk-forward folds; see :func:`walk_forward_splits`), then
    refit on everything. Each fold fits on its training part, picks the probability cut on that
    part's latest slice (best F1) and scores its test part at that cut.

    With folds, the scores are the mean over folds, ``spread`` their standard deviation and
    ``folds`` each fold's own; the saved cut is the latest fold's, the one nearest live data."""
    n, pos = len(y), int(y.sum())
    info = {"samples": n, "positives": pos}
    if pos < MIN_CLASS or n - pos < MIN_CLASS:
        return None, {**info, "skipped": f"needs at least {MIN_CLASS} of each class (has {pos} yes, {n - pos} no)"}
    done = [f for f in (_fold(X, y, when, *split, r) for split in walk_forward_splits(when, folds, test_fraction, gap)) if f]
    if not done:
        return None, {**info, "skipped": "the training part has only one class; label more of the earlier history"}
    scores = [s for s, _ in done]
    last, model = done[-1]
    test = dict(last)
    if folds > 1:
        for k in FOLD_KEYS:
            vals = [s[k] for s in scores if s.get(k) is not None]
            test[k] = float(np.mean(vals)) if vals else None
        test["spread"] = {k: float(np.std(v)) for k in SPREAD_KEYS
                          if len(v := [s[k] for s in scores if s.get(k) is not None]) > 1}
        test.update(test_samples=sum(s["test_samples"] for s in scores),
                    test_positives=sum(s["test_positives"] for s in scores), test_from=scores[0]["test_from"],
                    folds=scores, folds_asked=folds)
        if r is not None:
            test["picked"] = sum(s.get("picked", 0) for s in scores)
    te = walk_forward_splits(when, folds, test_fraction, gap)[-1][2]
    if len(np.unique(y[te])) == 2:
        # Held-out data only (the latest fold), with the model fit before it: the refit has seen it.
        test["importance"] = importance(model, X.iloc[te], y[te])
    final = _classifier().fit(X, y)
    return final, {**info, **test}


# ---- samples --------------------------------------------------------------

@dataclass
class Frames:
    """Candles and features of one timeframe, computed once per training run."""

    tf: Timeframe
    candles: pd.DataFrame
    feats: pd.DataFrame
    atr: pd.Series


def frames_for(m1: pd.DataFrame, tf: Timeframe, fp: FeatureParams) -> Frames:
    candles, feats = timeframe_features(m1, tf, fp)
    return Frames(tf, candles, feats, _atr(candles, fp.atr_length).reindex(feats.index))


def tag_samples(frames: list[Frames], labels: dict[str, list[dict]], reviewed: dict[str, list[dict]],
                tags: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(features, label matrix) of every candle with at least one known label."""
    xs, ys = [], []
    for fr in frames:
        matrix = label_matrix(fr.feats.index, labels.get(fr.tf.name, []), reviewed.get(fr.tf.name, []), tags)
        keep = matrix.notna().any(axis=1)
        xs.append(fr.feats[keep])
        ys.append(matrix[keep])
    if not xs:
        return pd.DataFrame(), pd.DataFrame(columns=tags)
    return pd.concat(xs), pd.concat(ys)


def zone_features(feats_row: pd.Series, atr: float, close: float, direction: str, top: float, bottom: float,
                  max_sl: float) -> dict | None:
    levels = plan_levels(direction, top, bottom, max_sl)
    if levels is None or not atr or np.isnan(atr):
        return None
    entry, stop = levels
    return {"zone_dir": 1.0 if direction == "bullish" else -1.0, "zone_body": (top - bottom) / atr,
            "zone_risk": abs(entry - stop) / atr, "zone_capped": float(top - bottom > max_sl),
            "zone_entry": (entry - close) / atr}


def outcome_samples(m1: pd.DataFrame, frames: list[Frames], labels: dict[str, list[dict]], params: TrainParams,
                    detector: DetectorParams) -> pd.DataFrame:
    """One row per order block (your labels, plus the detector's when asked) with its traded outcome."""
    plan = TradePlan(rr=params.rr, horizon_minutes=params.horizon_hours * 60, max_sl=detector.max_sl)
    rows = []
    for fr in frames:
        c = fr.candles
        blocks: dict[pd.Timestamp, tuple[str, float, float, pd.Timestamp, str]] = {}
        if params.outcome_from_detector and len(c) > 2 * detector.swing_length + 2:
            for lv in OrderBlockDetector(detector).detect(Context(c)):
                known = lv.confirmed_time + fr.tf.delta  # the break candle's close
                blocks[lv.time] = (lv.kind, lv.top, lv.bottom, known, "detector")
        for lab in labels.get(fr.tf.name, []):
            if lab["value"] != 1 or lab["tag"] not in OB_TAGS:
                continue
            span = c.loc[ts(lab["start"]):ts(lab["end"])]
            if span.empty:
                continue
            top = lab["top"] if lab.get("top") is not None else float(np.maximum(span["open"], span["close"]).max())
            bottom = lab["bottom"] if lab.get("bottom") is not None else float(np.minimum(span["open"], span["close"]).min())
            blocks[span.index[-1]] = (TAGS[lab["tag"]]["kind"], top, bottom, pd.NaT, "label")
        for t, (direction, top, bottom, known, origin) in blocks.items():
            if t not in fr.feats.index:
                continue
            row = fr.feats.loc[t]
            start = row["available_at"] if pd.isna(known) else max(known, row["available_at"])
            zone = zone_features(row, float(fr.atr.loc[t]), float(c.loc[t, "close"]), direction, top, bottom, detector.max_sl)
            if zone is None:
                continue
            entry, stop = plan_levels(direction, top, bottom, detector.max_sl)
            outcome, r = simulate(m1, direction, entry, stop, start, plan)
            if outcome not in ("win", "loss"):
                continue
            rows.append({**row.to_dict(), **zone, "available_at": start, "outcome": outcome, "r": r, "origin": origin})
    return pd.DataFrame(rows)


# ---- training ---------------------------------------------------------------

def train_bundle(m1: pd.DataFrame, labels: dict[str, list[dict]], reviewed: dict[str, list[dict]],
                 params: TrainParams, symbol: str, detector: DetectorParams, progress=lambda stage: None) -> ModelBundle:
    fp = params.features
    progress("Building features")
    frames = [frames_for(m1, TIMEFRAMES_BY_NAME[name], fp) for name in params.timeframes]
    X, Y = tag_samples(frames, labels, reviewed, params.tags)
    names = feature_names(X) if len(X) else []
    tag_models, tag_metrics = {}, {}
    for tag in params.tags:
        progress(f"Training {TAGS[tag]['title']}")
        known = Y[tag].notna().to_numpy() if len(Y) else np.array([], dtype=bool)
        if not known.any():
            tag_metrics[tag] = {"samples": 0, "positives": 0, "skipped": "no labels for this tag in reviewed ranges"}
            continue
        model, metrics = fit_eval(X.loc[known, names], Y[tag].to_numpy()[known].astype(int),
                                  X["available_at"].to_numpy()[known], params.test_fraction,
                                  folds=params.folds, gap=params.gap)
        tag_metrics[tag] = metrics
        if model is not None:
            tag_models[tag] = model

    outcome_model, outcome_metrics, outcome_names = None, None, []
    if any(t in OB_TAGS for t in params.tags):
        progress("Simulating order block trades")
        samples = outcome_samples(m1, frames, labels, params, detector)
        if len(samples):
            outcome_names = feature_names(frames[0].feats) + ZONE_FEATURES
            progress("Training the outcome model")
            y = (samples["outcome"] == "win").to_numpy().astype(int)
            outcome_model, outcome_metrics = fit_eval(samples[outcome_names], y, samples["available_at"].to_numpy(),
                                                      params.test_fraction, r=samples["r"].to_numpy(),
                                                      folds=params.folds, gap=params.gap)
            outcome_metrics["from_labels"] = int((samples["origin"] == "label").sum())
            outcome_metrics["from_detector"] = int((samples["origin"] == "detector").sum())
        else:
            outcome_metrics = {"samples": 0, "positives": 0, "skipped": "no finished order block trades in the history"}

    if not tag_models and outcome_model is None:
        reasons = "; ".join(f"{TAGS[t]['title']}: {m['skipped']}" for t, m in tag_metrics.items() if "skipped" in m)
        raise ValueError(f"Nothing to train yet. {reasons}".strip())

    now = datetime.now(timezone.utc)
    manifest = {
        "id": f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
        "name": params.name.strip() or f"{'/'.join(params.timeframes)} {now:%d %b %H:%M}",
        "author": params.author.strip(),
        "note": params.note.strip(),
        "created_at": now.isoformat(),
        "symbol": symbol,
        "timeframes": params.timeframes,
        "tags": {t: {"title": TAGS[t]["title"], **m, "trained": t in tag_models} for t, m in tag_metrics.items()},
        "outcome": outcome_metrics and {**outcome_metrics, "trained": outcome_model is not None},
        "params": {"lookback": params.lookback, "confirm": params.confirm, "rr": params.rr,
                   "horizon_hours": params.horizon_hours, "max_sl": detector.max_sl, "folds": params.folds,
                   "gap_minutes": int(params.gap.total_seconds() // 60)},
        "data": {"first": m1.index[0].isoformat(), "last": m1.index[-1].isoformat(), "m1_bars": len(m1),
                 "labels": sum(len(v) for v in labels.values())},
        "inputs": input_quantiles(frames),
        "sklearn": _sklearn_version(),
    }
    med = {"tags": medians(X[names]) if len(X) else {},
           "outcome": medians(samples[outcome_names]) if outcome_model is not None else {}}
    return ModelBundle(manifest, tag_models, outcome_model, names, outcome_names, med)


# Features watched for drift: volatility, and candle size relative to it.
INPUTS = {"atr_pct": "ATR", "body_abs": "candle body"}
QUANTILES = (0.1, 0.5, 0.9)


def _inputs_of(feats: pd.DataFrame) -> dict[str, pd.Series]:
    return {"atr_pct": feats["atr_pct"], "body_abs": feats["body_0"].abs()}


def input_quantiles(frames: list[Frames]) -> dict:
    """Per timeframe, the 10/50/90% quantiles of the drift-watched inputs over the training history."""
    out = {}
    for fr in frames:
        q = {}
        for k, v in _inputs_of(fr.feats).items():
            v = v.dropna()
            if len(v):
                q[k] = [float(x) for x in v.quantile(list(QUANTILES))]
        out[fr.tf.name] = q
    return out


def _sklearn_version() -> str:
    import sklearn

    return sklearn.__version__


# ---- prediction -------------------------------------------------------------

def predict_blocks(bundle: ModelBundle, m1: pd.DataFrame, tf: Timeframe, limit: int = 300,
                   threshold: float | None = None) -> list[dict]:
    """Candles of the last ``limit`` on ``tf`` that the model tags: probability at or above
    ``threshold``, or each tag's own cut from training when it is None. Each block carries
    ``why``: the three feature families that moved its probability most (empty for model
    files without training medians)."""
    p = bundle.manifest["params"]
    fp = FeatureParams(lookback=int(p["lookback"]), confirm=int(p["confirm"]))
    # Enough M1 for the candles, their lookback and two higher timeframes of context.
    need = (limit + fp.lookback + fp.confirm + 20) * tf.minutes + 3 * 240
    fr = frames_for(m1.tail(need), tf, fp)
    feats = fr.feats.tail(limit)
    if feats.empty:
        return []
    c = fr.candles.reindex(feats.index)
    out = []
    probs = {tag: model.predict_proba(feats[bundle.features])[:, 1] for tag, model in bundle.models.items()}
    for tag, pr in probs.items():
        info = TAGS.get(tag)
        if info is None:
            continue
        cut = threshold if threshold is not None else float(bundle.manifest["tags"].get(tag, {}).get("threshold", 0.5))
        hits = np.flatnonzero(pr >= cut)
        why = contributions(bundle.models[tag], feats[bundle.features].iloc[hits], bundle.medians.get("tags") or {})
        for n, i in enumerate(hits):
            t = feats.index[i]
            row = c.iloc[i]
            if info["shape"] == "body":
                top, bottom = max(row["open"], row["close"]), min(row["open"], row["close"])
            elif info["shape"] == "high":
                top = bottom = row["high"]
            else:
                top = bottom = row["low"]
            block = {"id": f"{tf.name}:{tag}:{unix(t)}", "tag": tag, "title": info["title"], "timeframe": tf.name,
                     "time_unix": unix(t), "available_unix": unix(feats["available_at"].iloc[i]),
                     "prob": float(pr[i]), "top": float(top), "bottom": float(bottom), "outcome_prob": None,
                     "why": why[n]}
            if tag in OB_TAGS and bundle.outcome is not None:
                zone = zone_features(feats.iloc[i], float(fr.atr.loc[t]), float(row["close"]), info["kind"], top, bottom,
                                     float(p.get("max_sl", 3.0)))
                if zone is not None:
                    x = pd.DataFrame([{**feats.iloc[i].to_dict(), **zone}])[bundle.outcome_features]
                    block["outcome_prob"] = float(bundle.outcome.predict_proba(x)[0, 1])
            out.append(block)
    out.sort(key=lambda b: b["time_unix"])
    return out


# ---- review queue: where the model is least sure --------------------------------------

def queue_scores(bundle: ModelBundle, m1: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    """Every candle of the history on ``tf`` scored by every tag of the model: one row per
    (candle, tag) with the probability, the tag's cut and ``distance`` = |prob - cut|, the
    smallest first. Labels near the cut teach the model the most."""
    p = bundle.manifest["params"]
    fp = FeatureParams(lookback=int(p["lookback"]), confirm=int(p["confirm"]))
    fr = frames_for(m1, tf, fp)
    feats = fr.feats
    cols = ["time_unix", "tag", "prob", "cut", "distance"]
    if feats.empty:
        return pd.DataFrame(columns=cols)
    times = np.array([unix(t) for t in feats.index], dtype=np.int64)
    parts = []
    for tag, model in bundle.models.items():
        if tag not in TAGS:
            continue
        cut = float(bundle.manifest["tags"].get(tag, {}).get("threshold", 0.5))
        pr = model.predict_proba(feats[bundle.features])[:, 1]
        parts.append(pd.DataFrame({"time_unix": times, "tag": tag, "prob": pr, "cut": cut, "distance": np.abs(pr - cut)}))
    if not parts:
        return pd.DataFrame(columns=cols)
    return pd.concat(parts, ignore_index=True).sort_values(["distance", "time_unix"], kind="stable", ignore_index=True)


# ---- comparing models on one window ------------------------------------------------

def _scores(y: np.ndarray, p: np.ndarray, cut: float, r: np.ndarray | None = None) -> dict:
    from sklearn.metrics import precision_score, recall_score, roc_auc_score

    pred = p >= cut
    out = {"samples": int(len(y)), "positives": int(y.sum()), "threshold": cut}
    if not len(y):
        return out
    out["precision"] = float(precision_score(y, pred, zero_division=0))
    out["recall"] = float(recall_score(y, pred, zero_division=0))
    if len(np.unique(y)) == 2:
        out["auc"] = float(roc_auc_score(y, p))
    out["base_rate"] = float(y.mean())
    if r is not None:
        out["avg_r_all"] = float(r.mean())
        out["picked"] = int(pred.sum())
        out["avg_r_picked"] = float(r[pred].mean()) if pred.any() else None
    return out


def score_window(bundles: list[ModelBundle], m1: pd.DataFrame, labels: dict[str, list[dict]],
                 reviewed: dict[str, list[dict]], detector: DetectorParams) -> dict:
    """Score several models on the same candles: those that became known after the newest model's
    training data ends, on the timeframes they share. Each model trained on data up to its own
    ``data.last``, so none of them saw these candles or their labels. Scores use each model's own
    cut, and the outcome model is scored on the order block trades that finished in the window."""
    import dataclasses

    if len(bundles) < 2:
        raise ValueError("Pick at least two models to compare")
    shared = [tf for tf in bundles[0].manifest["timeframes"] if all(tf in b.manifest["timeframes"] for b in bundles)]
    if not shared:
        raise ValueError("These models share no timeframe, so there is no common window to score them on")
    start = max(pd.Timestamp(b.manifest["data"]["last"]) for b in bundles)
    tags = sorted({t for b in bundles for t in b.models})
    cache: dict = {}
    out: dict = {"from": start.isoformat(), "to": m1.index[-1].isoformat(), "timeframes": shared, "models": {}}
    labelled = 0
    for b in bundles:
        p = b.manifest["params"]
        fp = FeatureParams(lookback=int(p["lookback"]), confirm=int(p["confirm"]))
        frames = []
        for name in shared:
            key = (name, fp)
            if key not in cache:
                cache[key] = frames_for(m1, TIMEFRAMES_BY_NAME[name], fp)
            frames.append(cache[key])
        X, Y = tag_samples(frames, labels, reviewed, tags)
        after = (X["available_at"] > start).to_numpy() if len(X) else np.array([], dtype=bool)
        res: dict = {"tags": {}, "outcome": None}
        for tag, model in b.models.items():
            known = after & Y[tag].notna().to_numpy() if len(Y) else after
            if not known.any():
                res["tags"][tag] = {"samples": 0, "positives": 0}
                continue
            prob = model.predict_proba(X.loc[known, b.features])[:, 1]
            cut = float(b.manifest["tags"].get(tag, {}).get("threshold", 0.5))
            res["tags"][tag] = _scores(Y[tag].to_numpy()[known].astype(int), prob, cut)
            labelled = max(labelled, int(known.sum()))
        if b.outcome is not None:
            plan = TrainParams(rr=float(p.get("rr", 2.0)), horizon_hours=int(p.get("horizon_hours", 72)))
            det = dataclasses.replace(detector, max_sl=float(p.get("max_sl", detector.max_sl)))
            samples = outcome_samples(m1, frames, labels, plan, det)
            if len(samples):
                samples = samples[samples["available_at"] > start]
            if len(samples):
                prob = b.outcome.predict_proba(samples[b.outcome_features])[:, 1]
                cut = float((b.manifest.get("outcome") or {}).get("threshold", 0.5))
                res["outcome"] = _scores((samples["outcome"] == "win").to_numpy().astype(int), prob, cut,
                                         r=samples["r"].to_numpy())
                labelled = max(labelled, len(samples))
        out["models"][b.id] = res
    if not labelled:
        raise ValueError(f"Nothing to score after {start:%Y-%m-%d %H:%M}, where the newest model's training data "
                         "ends: label and mark reviewed some candles after it (or wait for order block trades "
                         "to finish), then score again")
    return out
