"""The Lab as the server sees it: labels in the database, model files on disk,
one training run at a time on a background thread."""

from __future__ import annotations

import importlib.util
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..detectors.base import DetectorParams
from ..storage import Store, lab_labels_table, lab_reviewed_table, lab_reviews_table
from ..timeframes import TIMEFRAMES_BY_NAME
from .dataset import ts, unix
from .model import ModelBundle, ModelFileError, load, load_bytes, read_manifest
from .outcome import TradePlan, plan_levels, simulate
from .tags import OB_TAGS, TAGS
from .train import TrainParams, frames_for, train_bundle

log = logging.getLogger(__name__)

ACTIVE_KEY = "lab_active_model"
HISTORY_BARS = 400_000  # about a year of M1 bars from the database
HISTORY_TTL = 600  # seconds before the stored history is read again


def ml_available() -> tuple[bool, str | None]:
    missing = [m for m in ("sklearn", "pyarrow") if importlib.util.find_spec(m) is None]
    return (False, "Install the ML extra: uv sync --extra ml") if missing else (True, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Lab:
    def __init__(self, store: Store, models_dir: str | Path, detector: DetectorParams):
        self.store = store
        self.models_dir = Path(models_dir)
        self.detector = detector
        self._history: dict = {}
        self._staged: dict[str, bytes] = {}
        self._active: ModelBundle | None = None
        self._lock = threading.Lock()
        self.training = {"running": False, "stage": None, "error": None, "last": None, "started_at": None}

    # ---- price history ---------------------------------------------------
    def history(self, source: str, symbol: str, live: pd.DataFrame | None) -> pd.DataFrame | None:
        """Stored M1 bars (cached for a while) joined with the engine's live buffer."""
        key = (source, symbol)
        cached = self._history.get(key)
        if cached is None or time.monotonic() - cached[0] > HISTORY_TTL:
            stored = self.store.load_bars(source, symbol, HISTORY_BARS)
            cached = (time.monotonic(), stored)
            self._history[key] = cached
        stored = cached[1]
        if live is None or live.empty:
            return stored if len(stored) else None
        if not len(stored):
            return live
        both = pd.concat([stored[stored.index < live.index[0]], live])
        return both[~both.index.duplicated(keep="last")]

    # ---- labels -------------------------------------------------------------
    def labels(self, symbol: str, tf: str | None = None, start: int | None = None, end: int | None = None) -> list[dict]:
        return self.store.lab_rows(lab_labels_table, symbol, tf, start, end)

    def reviewed(self, symbol: str, tf: str | None = None, start: int | None = None, end: int | None = None) -> list[dict]:
        return self.store.lab_rows(lab_reviewed_table, symbol, tf, start, end)

    def by_tf(self, rows: list[dict]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["timeframe"], []).append(r)
        return out

    def add_label(self, symbol: str, body: dict, origin: str = "manual", label_id: str | None = None) -> dict:
        tf, tag = body.get("timeframe"), body.get("tag")
        if tf not in TIMEFRAMES_BY_NAME:
            raise ValueError(f"Unknown timeframe {tf!r}")
        if tag not in TAGS:
            raise ValueError(f"Unknown tag {tag!r}")
        start, end = int(body["start"]), int(body.get("end", body["start"]))
        if end < start:
            start, end = end, start
        row = {"id": label_id or uuid.uuid4().hex, "symbol": symbol, "timeframe": tf, "tag": tag,
               "value": 0 if body.get("value") in (0, False, "0") else 1, "start": start, "end": end,
               "top": body.get("top"), "bottom": body.get("bottom"),
               "origin": origin if origin in ("manual", "detector", "review") else "manual", "created_at": _now()}
        self.store.lab_put(lab_labels_table, row)
        return row

    def add_reviewed(self, symbol: str, body: dict) -> dict:
        tf = body.get("timeframe")
        if tf not in TIMEFRAMES_BY_NAME:
            raise ValueError(f"Unknown timeframe {tf!r}")
        tags = [t for t in body.get("tags") or list(TAGS) if t in TAGS]
        if not tags:
            raise ValueError("Pick at least one tag")
        start, end = sorted((int(body["start"]), int(body["end"])))
        row = {"id": uuid.uuid4().hex, "symbol": symbol, "timeframe": tf, "start": start, "end": end, "tags": tags,
               "created_at": _now()}
        self.store.lab_put(lab_reviewed_table, row)
        return row

    def delete(self, kind: str, row_id: str) -> bool:
        table = {"labels": lab_labels_table, "reviewed": lab_reviewed_table}[kind]
        return self.store.lab_delete(table, row_id)

    def counts(self, symbol: str) -> dict:
        """Per timeframe: yes / no labels per tag and how many reviewed ranges."""
        out: dict[str, dict] = {}
        for lab in self.labels(symbol):
            tf = out.setdefault(lab["timeframe"], {"tags": {}, "reviewed": 0})
            c = tf["tags"].setdefault(lab["tag"], {"yes": 0, "no": 0})
            c["yes" if lab["value"] else "no"] += 1
        for r in self.reviewed(symbol):
            out.setdefault(r["timeframe"], {"tags": {}, "reviewed": 0})["reviewed"] += 1
        return out

    # ---- training -------------------------------------------------------------
    def start_training(self, m1: pd.DataFrame, symbol: str, params: TrainParams) -> None:
        with self._lock:
            if self.training["running"]:
                raise RuntimeError("A model is already training")
            self.training.update(running=True, stage="Starting", error=None, started_at=_now())
        labels = self.by_tf(self.labels(symbol))
        reviewed = self.by_tf(self.reviewed(symbol))
        threading.Thread(target=self._train, args=(m1, labels, reviewed, params, symbol), daemon=True,
                         name="lab-train").start()

    def _train(self, m1, labels, reviewed, params, symbol) -> None:
        try:
            bundle = train_bundle(m1, labels, reviewed, params, symbol, self.detector,
                                  progress=lambda stage: self.training.update(stage=stage))
            bundle.save(self._path(bundle.id))
            self.training["last"] = bundle.manifest
        except ValueError as exc:
            self.training["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - shown in the Lab
            log.exception("training failed")
            self.training["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self.training.update(running=False, stage=None)

    # ---- model files ------------------------------------------------------------
    def _path(self, model_id: str) -> Path:
        safe = "".join(ch for ch in model_id if ch.isalnum() or ch in "-_")
        if not safe:
            raise ModelFileError("Bad model id")
        return self.models_dir / f"{safe}.zip"

    def models(self) -> list[dict]:
        out = []
        for path in sorted(self.models_dir.glob("*.zip"), reverse=True):
            try:
                out.append(read_manifest(path.read_bytes()))
            except (OSError, ModelFileError) as exc:
                log.warning("skipping model file %s: %s", path, exc)
        return out

    @property
    def active_id(self) -> str | None:
        v = self.store.get_setting(ACTIVE_KEY)
        return v.get("id") if v else None

    def active(self) -> ModelBundle | None:
        model_id = self.active_id
        if not model_id:
            return None
        if self._active is None or self._active.id != model_id:
            try:
                self._active = load(self._path(model_id))
            except (OSError, ModelFileError) as exc:
                log.warning("active model %s can't be loaded: %s", model_id, exc)
                return None
        return self._active

    def activate(self, model_id: str | None) -> None:
        if model_id is None:
            self.store.delete_setting(ACTIVE_KEY)
            return
        if not self._path(model_id).is_file():
            raise ModelFileError("No such model")
        self._active = load(self._path(model_id))  # proves it loads before it becomes active
        self.store.set_setting(ACTIVE_KEY, {"id": model_id})

    def delete_model(self, model_id: str) -> bool:
        path = self._path(model_id)
        if not path.is_file():
            return False
        if self.active_id == model_id:
            self.activate(None)
        path.unlink()
        return True

    def export(self, model_id: str) -> bytes:
        path = self._path(model_id)
        if not path.is_file():
            raise ModelFileError("No such model")
        return path.read_bytes()

    def stage_import(self, data: bytes) -> dict:
        """Read the manifest only; the pickle waits until :meth:`confirm_import`."""
        manifest = read_manifest(data)
        token = secrets.token_hex(8)
        while len(self._staged) >= 3:
            self._staged.pop(next(iter(self._staged)))
        self._staged[token] = data
        exists = self._path(manifest["id"]).is_file()
        return {"token": token, "manifest": manifest, "exists": exists}

    def confirm_import(self, token: str) -> dict:
        data = self._staged.pop(token, None)
        if data is None:
            raise ModelFileError("That upload expired; choose the file again")
        bundle = load_bytes(data)  # safe unpickler + hash check
        path = self._path(bundle.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return bundle.manifest

    # ---- reviews of model output --------------------------------------------------
    def add_review(self, symbol: str, body: dict, m1: pd.DataFrame | None) -> dict:
        bundle = self.active()
        tf, tag, verdict = body.get("timeframe"), body.get("tag"), body.get("verdict")
        if tf not in TIMEFRAMES_BY_NAME or tag not in TAGS:
            raise ValueError("Unknown timeframe or tag")
        if verdict not in ("valid", "invalid"):
            raise ValueError("verdict is valid or invalid")
        t = int(body["time_unix"])
        model_id = str(body.get("model_id") or (bundle.id if bundle else "none"))
        outcome, r = None, None
        if tag in OB_TAGS and m1 is not None and body.get("top") is not None:
            params = bundle.manifest["params"] if bundle else {}
            plan = TradePlan(rr=float(params.get("rr", 2.0)), horizon_minutes=int(params.get("horizon_hours", 72)) * 60,
                             max_sl=float(params.get("max_sl", self.detector.max_sl)))
            kind = TAGS[tag]["kind"]
            levels = plan_levels(kind, float(body["top"]), float(body["bottom"]), plan.max_sl)
            if levels:
                start = ts(int(body.get("available_unix") or t))
                outcome, r = simulate(m1, kind, *levels, start, plan)
        row = {"id": f"{model_id}:{tf}:{tag}:{t}", "symbol": symbol, "model_id": model_id, "timeframe": tf, "tag": tag,
               "time": t, "prob": float(body.get("prob") or 0), "verdict": verdict, "outcome": outcome, "r": r,
               "created_at": _now()}
        self.store.lab_put(lab_reviews_table, row)
        # The verdict is also a label: the next training run learns from it.
        self.add_label(symbol, {"timeframe": tf, "tag": tag, "start": t, "end": t, "value": int(verdict == "valid"),
                                "top": body.get("top"), "bottom": body.get("bottom")},
                       origin="review", label_id=f"review:{tf}:{tag}:{t}")
        return row

    def reviews(self, symbol: str, tf: str | None = None) -> list[dict]:
        return self.store.lab_rows(lab_reviews_table, symbol, tf)

    def feedback_frame(self, symbol: str, m1: pd.DataFrame) -> pd.DataFrame:
        """Reviews as (state, action, rewards): the candle's features, the tag the model called,
        the human verdict (+1 / -1) and the market result in R. The start of an RL dataset."""
        rows = self.reviews(symbol)
        if not rows:
            return pd.DataFrame()
        bundle = self.active()
        p = bundle.manifest["params"] if bundle else {}
        fp = TrainParams(lookback=int(p.get("lookback", 10)), confirm=int(p.get("confirm", 3))).features
        out = []
        for tf, group in pd.DataFrame(rows).groupby("timeframe"):
            fr = frames_for(m1, TIMEFRAMES_BY_NAME[tf], fp)
            for r in group.to_dict("records"):
                t = ts(r["time"])
                feats = fr.feats.loc[t].drop("available_at").to_dict() if t in fr.feats.index else {}
                out.append({"time": t, "timeframe": tf, "tag": r["tag"], "model_id": r["model_id"], "prob": r["prob"],
                            "verdict": r["verdict"], "human_reward": 1 if r["verdict"] == "valid" else -1,
                            "outcome": r["outcome"], "r": r["r"], **feats})
        return pd.DataFrame(out).sort_values("time")

    def status(self, symbol: str) -> dict:
        ok, reason = ml_available()
        return {"available": ok, "reason": reason, "tags": [{"id": t, "title": i["title"], "shape": i["shape"]}
                                                            for t, i in TAGS.items()],
                "counts": self.counts(symbol), "training": dict(self.training), "models": self.models(),
                "active": self.active_id}


def history_bounds(m1: pd.DataFrame | None) -> dict | None:
    if m1 is None or m1.empty:
        return None
    return {"first_unix": unix(m1.index[0]), "last_unix": unix(m1.index[-1]), "bars": len(m1)}
