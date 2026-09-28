"""The Lab as the server sees it: labels in the database, model files on disk,
one training run at a time on a background thread."""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..detectors.base import DetectorParams
from ..storage import Store, lab_labels_table, lab_reviewed_table, lab_reviews_table
from ..timeframes import TIMEFRAMES_BY_NAME
from .bars import HISTORY_BARS_DEFAULT, HISTORY_BARS_MAX, LAB_DATA_KEY, Backfill, BarsFileError, ParsedBars, parse_bars
from .dataset import ts, unix
from .model import ModelBundle, ModelFileError, load, load_bytes, read_manifest
from .outcome import TradePlan, plan_levels, simulate
from .tags import OB_TAGS, TAGS
from . import drift, signing
from .train import TrainingCancelled, TrainParams, frames_for, queue_scores, score_window, train_bundle

log = logging.getLogger(__name__)

ACTIVE_KEY = "lab_active_model"
HISTORY_TTL = 600  # seconds before the stored history is read again
SCORECARD_TTL = 60  # seconds a live scorecard is reused
LABELS_FORMAT = "wednesday-labels"
LABELS_FORMAT_VERSION = 1
MAX_LABELS_BYTES = 50 * 1024 * 1024


class LabelsFileError(ValueError):
    pass


def _label_key(r: dict) -> tuple:
    return (r["timeframe"], r["tag"], r["start"], r["end"], r["value"])


def _reviewed_key(r: dict) -> tuple:
    return (r["timeframe"], r["start"], r["end"], tuple(sorted(r["tags"])))


def _clean_label(r: dict) -> dict:
    tf, tag = r.get("timeframe"), r.get("tag")
    if tf not in TIMEFRAMES_BY_NAME or tag not in TAGS:
        raise LabelsFileError(f"A label has an unknown timeframe or tag ({tf!r}, {tag!r})")
    start, end = sorted((int(r["start"]), int(r.get("end", r["start"]))))
    num = lambda v: None if v is None else float(v)  # noqa: E731
    return {"id": str(r.get("id") or uuid.uuid4().hex)[:36], "timeframe": tf, "tag": tag,
            "value": 1 if r.get("value") in (1, True, "1") else 0, "start": start, "end": end,
            "top": num(r.get("top")), "bottom": num(r.get("bottom")),
            "origin": r.get("origin") if r.get("origin") in ("manual", "detector", "review") else "manual",
            "created_at": str(r.get("created_at") or _now())}


def _clean_reviewed(r: dict) -> dict:
    tf = r.get("timeframe")
    tags = [t for t in r.get("tags") or [] if t in TAGS]
    if tf not in TIMEFRAMES_BY_NAME or not tags:
        raise LabelsFileError(f"A reviewed range has an unknown timeframe or no known tags ({tf!r})")
    start, end = sorted((int(r["start"]), int(r["end"])))
    return {"id": str(r.get("id") or uuid.uuid4().hex)[:36], "timeframe": tf, "start": start, "end": end,
            "tags": tags, "created_at": str(r.get("created_at") or _now())}


def parse_labels(data: bytes) -> dict:
    """A labels file (from :meth:`Lab.export_labels`), checked row by row."""
    if len(data) > MAX_LABELS_BYTES:
        raise LabelsFileError("The file is larger than 50 MB")
    try:
        doc = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LabelsFileError("Not a Wednesday labels file (not JSON)") from exc
    if not isinstance(doc, dict) or doc.get("format") != LABELS_FORMAT:
        raise LabelsFileError("Not a Wednesday labels file")
    if int(doc.get("version", 0)) > LABELS_FORMAT_VERSION:
        raise LabelsFileError("This file was made by a newer Wednesday; update to import it")
    try:
        labels = [_clean_label(r) for r in doc.get("labels") or []]
        reviewed = [_clean_reviewed(r) for r in doc.get("reviewed") or []]
    except LabelsFileError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise LabelsFileError(f"A row can't be read: {exc}") from exc
    return {"symbol": str(doc.get("symbol") or ""), "labels": labels, "reviewed": reviewed}


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
        self.training = {"running": False, "stage": None, "error": None, "last": None, "started_at": None,
                         "cancelled": False}
        self._cancel = threading.Event()
        self._scorecard: tuple | None = None
        self._signatures: dict[str, tuple] = {}
        self._queue: dict[tuple, pd.DataFrame] = {}
        self.backfill = Backfill()
        self._staged_bars: dict[str, ParsedBars] = {}
        self._staged_labels: dict[str, dict] = {}
        saved = store.get_setting(LAB_DATA_KEY) or {}
        self.history_bars = int(saved.get("history_bars") or HISTORY_BARS_DEFAULT)
        store.lab_runs_interrupted(_now())

    # ---- price history ---------------------------------------------------
    def set_history_bars(self, bars: int) -> None:
        """How many stored M1 bars the Lab loads (labelling, training, the dataset)."""
        if not 10_000 <= bars <= HISTORY_BARS_MAX:
            raise ValueError(f"History must be between 10,000 and {HISTORY_BARS_MAX:,} M1 bars")
        self.history_bars = bars
        self.store.set_setting(LAB_DATA_KEY, {"history_bars": bars})
        self.forget_history()

    def forget_history(self) -> None:
        """Drop the cached history, after bars were imported or backfilled."""
        self._history.clear()

    def stage_bars(self, data: bytes) -> dict:
        """Read an M1 file and keep it until :meth:`take_bars`; returns the preview."""
        parsed = parse_bars(data)
        token = secrets.token_hex(8)
        while len(self._staged_bars) >= 2:
            self._staged_bars.pop(next(iter(self._staged_bars)))
        self._staged_bars[token] = parsed
        return {"token": token, **parsed.preview()}

    def take_bars(self, token: str) -> ParsedBars:
        parsed = self._staged_bars.pop(token, None)
        if parsed is None:
            raise BarsFileError("That upload expired; choose the file again")
        return parsed

    def history(self, source: str, symbol: str, live: pd.DataFrame | None) -> pd.DataFrame | None:
        """Stored M1 bars (cached for a while) joined with the engine's live buffer."""
        key = (source, symbol)
        cached = self._history.get(key)
        if cached is None or time.monotonic() - cached[0] > HISTORY_TTL:
            stored = self.store.load_bars(source, symbol, self.history_bars)
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
        now = _now()
        row = {"id": label_id or uuid.uuid4().hex, "symbol": symbol, "timeframe": tf, "tag": tag,
               "value": 0 if body.get("value") in (0, False, "0") else 1, "start": start, "end": end,
               "top": body.get("top"), "bottom": body.get("bottom"),
               "origin": origin if origin in ("manual", "detector", "review") else "manual", "created_at": now,
               "updated_at": now, "changed_by": "review" if origin == "review" else "manual"}
        self.store.lab_put(lab_labels_table, row)
        return row

    def batch(self, symbol: str, body: dict) -> dict:
        """Several label and reviewed-range changes at once: bulk edits, and undo / redo.

        ``add_labels`` / ``add_reviewed`` rows with an ``id`` are put back exactly as given (what an
        undo of a delete sends); rows without one are new. ``delete_labels`` / ``delete_reviewed``
        are ids. Returns the rows written and the full rows deleted, so the client can undo it.
        Only rows of ``symbol`` are deleted."""
        out: dict = {"labels": [], "reviewed": [], "deleted_labels": [], "deleted_reviewed": []}
        for kind, table in (("labels", lab_labels_table), ("reviewed", lab_reviewed_table)):
            ids = [str(i) for i in body.get(f"delete_{kind}") or []]
            rows = [r for r in self.store.lab_get(table, ids) if r["symbol"] == symbol]
            for r in rows:
                self.store.lab_delete(table, r["id"])
            out[f"deleted_{kind}"] = rows
        for item in body.get("add_labels") or []:
            if item.get("id"):
                row = {**_clean_label(item), "symbol": symbol, "updated_at": item.get("updated_at"),
                       "changed_by": item.get("changed_by")}
                self._check_owner(lab_labels_table, row["id"], symbol)
                self.store.lab_put(lab_labels_table, row)
            else:
                row = self.add_label(symbol, item, item.get("origin") or "manual")
            out["labels"].append(row)
        for item in body.get("add_reviewed") or []:
            if item.get("id"):
                row = {**_clean_reviewed(item), "symbol": symbol}
                self._check_owner(lab_reviewed_table, row["id"], symbol)
                self.store.lab_put(lab_reviewed_table, row)
            else:
                row = self.add_reviewed(symbol, item)
            out["reviewed"].append(row)
        return out

    def _check_owner(self, table, row_id: str, symbol: str) -> None:
        if any(r["symbol"] != symbol for r in self.store.lab_get(table, [row_id])):
            raise ValueError("That id belongs to another symbol")

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

    # ---- labels file: back up, move to another machine, share ------------------------
    def export_labels(self, symbol: str) -> dict:
        """Every label and reviewed range of ``symbol``, as a labels file."""
        strip = lambda rows: [{k: v for k, v in r.items() if k != "symbol"} for r in rows]  # noqa: E731
        return {"format": LABELS_FORMAT, "version": LABELS_FORMAT_VERSION, "symbol": symbol, "exported_at": _now(),
                "labels": strip(self.labels(symbol)), "reviewed": strip(self.reviewed(symbol))}

    def _merge(self, table, incoming: list[dict], symbol: str, key) -> tuple[list[dict], dict]:
        """Rows to write into ``symbol`` and the counts: same id updates, the same label under another
        id is skipped. An id that belongs to another symbol gets a new one, so it stays untouched."""
        existing = self.store.lab_rows(table, symbol)
        ids = {r["id"] for r in existing}
        seen = {key(r) for r in existing}
        taken = self.store.lab_ids(table, [r["id"] for r in incoming if r["id"] not in ids])
        rows, counts = [], {"new": 0, "updated": 0, "skipped": 0}
        for r in incoming:
            if r["id"] in ids:
                counts["updated"] += 1
            elif key(r) in seen:
                counts["skipped"] += 1
                continue
            else:
                counts["new"] += 1
                if r["id"] in taken:
                    r = {**r, "id": uuid.uuid4().hex}
            seen.add(key(r))
            rows.append({**r, "symbol": symbol})
        return rows, counts

    def stage_labels(self, data: bytes, symbol: str) -> dict:
        """Read a labels file and keep it until :meth:`confirm_labels`; returns what would change."""
        parsed = parse_labels(data)
        token = secrets.token_hex(8)
        while len(self._staged_labels) >= 3:
            self._staged_labels.pop(next(iter(self._staged_labels)))
        self._staged_labels[token] = parsed
        return {"token": token, "symbol": parsed["symbol"], "matches": parsed["symbol"] == symbol,
                "labels": self._merge(lab_labels_table, parsed["labels"], symbol, _label_key)[1],
                "reviewed": self._merge(lab_reviewed_table, parsed["reviewed"], symbol, _reviewed_key)[1]}

    def confirm_labels(self, token: str, symbol: str, map_symbol: bool = False) -> dict:
        """Merge a staged labels file into ``symbol``. A file of another symbol needs ``map_symbol``."""
        parsed = self._staged_labels.get(token)
        if parsed is None:
            raise LabelsFileError("That upload expired; choose the file again")
        if parsed["symbol"] != symbol and not map_symbol:
            raise LabelsFileError(f"The file holds labels of {parsed['symbol'] or 'an unnamed symbol'}, not {symbol}; "
                                  "confirm that they should go to this symbol")
        del self._staged_labels[token]
        out = {}
        now = _now()
        for name, table, key in (("labels", lab_labels_table, _label_key), ("reviewed", lab_reviewed_table, _reviewed_key)):
            rows, out[name] = self._merge(table, parsed[name], symbol, key)
            for row in rows:
                if table is lab_labels_table:
                    row = {**row, "updated_at": now, "changed_by": "import"}
                self.store.lab_put(table, row)
        return out

    # ---- training -------------------------------------------------------------
    def start_training(self, m1: pd.DataFrame, symbol: str, params: TrainParams, feed: dict | None = None) -> None:
        """Train on a background thread. ``feed`` ({source, clock}) goes into the manifest so an
        import elsewhere can tell whether its bar times mean the same thing."""
        with self._lock:
            if self.training["running"]:
                raise RuntimeError("A model is already training")
            self._cancel.clear()
            self.training.update(running=True, stage="Starting", error=None, started_at=_now(), cancelled=False)
        run = {"id": uuid.uuid4().hex, "symbol": symbol, "started_at": self.training["started_at"], "finished_at": None,
               "status": "running", "error": None, "model_id": None, "params": asdict(params)}
        self.store.lab_run_put(run)
        labels = self.by_tf(self.labels(symbol))
        reviewed = self.by_tf(self.reviewed(symbol))
        threading.Thread(target=self._train, args=(m1, labels, reviewed, params, symbol, feed, run), daemon=True,
                         name="lab-train").start()

    def cancel_training(self) -> bool:
        """Ask the running training to stop at its next stage; nothing is saved. False when idle."""
        if not self.training["running"]:
            return False
        self._cancel.set()
        self.training["stage"] = "Stopping"
        return True

    def _progress(self, stage: str) -> None:
        if self._cancel.is_set():
            raise TrainingCancelled
        self.training["stage"] = stage

    def _train(self, m1, labels, reviewed, params, symbol, feed, run) -> None:
        try:
            bundle = train_bundle(m1, labels, reviewed, params, symbol, self.detector, progress=self._progress)
            self._progress("Saving")  # the last chance to stop before a file exists
            if feed:
                bundle.manifest["feed"] = feed
            key_path = os.environ.get("XAU_SIGNING_KEY")
            if key_path:  # the trainer's own key: every model it makes goes out signed
                path = self._path(bundle.id)
                data = signing.sign(bundle.to_bytes(), signing.load_private_key(key_path))
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            else:
                bundle.save(self._path(bundle.id))
            self.training["last"] = bundle.manifest
            run.update(status="done", model_id=bundle.id)
        except TrainingCancelled:
            self.training["cancelled"] = True
            run.update(status="cancelled")
        except ValueError as exc:
            self.training["error"] = str(exc)
            run.update(status="error", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - shown in the Lab
            log.exception("training failed")
            self.training["error"] = f"{type(exc).__name__}: {exc}"
            run.update(status="error", error=self.training["error"])
        finally:
            run["finished_at"] = _now()
            try:
                self.store.lab_run_put(run)
            except Exception:  # noqa: BLE001 - the run's outcome still shows until a restart
                log.exception("can't save the training run")
            self.training.update(running=False, stage=None)

    def runs(self) -> list[dict]:
        return self.store.lab_runs()

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
                data = path.read_bytes()
                out.append({**read_manifest(data), "signature": self._signature(path, data)})
            except (OSError, ModelFileError) as exc:
                log.warning("skipping model file %s: %s", path, exc)
        return out

    # ---- signatures -----------------------------------------------------------------
    def trust(self) -> dict:
        """The user's trusted public keys and whether only models signed by a trusted key load."""
        saved = self.store.get_setting(signing.TRUST_KEY) or {}
        builtin = [{"name": signing.BUILTIN_NAMES.get(k, k), "public_key": v, "builtin": True}
                   for k, v in signing.BUILTIN_KEYS.items()]
        keys = [{**k, "key_id": signing.key_id(signing.parse_public_key(k["public_key"])), "builtin": False}
                for k in saved.get("keys") or []]
        for k in builtin:
            k["key_id"] = signing.key_id(signing.parse_public_key(k["public_key"]))
        return {"keys": builtin + keys, "only_signed": bool(saved.get("only_signed"))}

    def set_trust(self, body: dict) -> dict:
        keys = []
        for k in body.get("keys") or []:
            if k.get("builtin"):
                continue
            name = str(k.get("name") or "").strip()[:60]
            if not name:
                raise ValueError("Give each trusted key a name")
            raw = signing.parse_public_key(str(k.get("public_key") or ""))  # SigningError is a ValueError
            keys.append({"name": name, "public_key": base64.b64encode(raw).decode()})
        self.store.set_setting(signing.TRUST_KEY, {"keys": keys, "only_signed": bool(body.get("only_signed"))})
        self._signatures.clear()
        return self.trust()

    def _trusted(self) -> dict[bytes, str]:
        return signing.trusted_keys((self.store.get_setting(signing.TRUST_KEY) or {}).get("keys"))

    def _signature(self, path: Path, data: bytes | None = None) -> dict:
        """Signature state of a model file, cached until the file changes."""
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._signatures.get(str(path))
        if hit and hit[0] == stamp:
            return hit[1]
        state = signing.verify(data if data is not None else path.read_bytes(), self._trusted())
        self._signatures[str(path)] = (stamp, state)
        return state

    def _check_signature(self, state: dict, trust_file: bool) -> None:
        """Refuse what may not load: a broken signature always, anything not signed by a trusted
        key with "only signed" on, and an unsigned or unknown file the user didn't vouch for."""
        if state["state"] == "invalid":
            raise ModelFileError(f"Not loading it: {state['reason']}")
        if state["state"] == "trusted":
            return
        if self.trust()["only_signed"]:
            raise ModelFileError("Only models signed by a trusted key load (turn that off in Settings > Lab)")
        if not trust_file:
            raise ModelFileError("This file isn't signed by a trusted key; confirm that you trust it to load it")

    # ---- review queue ---------------------------------------------------------------
    def queue(self, symbol: str, m1: pd.DataFrame, tf: str, tag: str | None = None, scope: str = "all",
              limit: int = 200) -> dict:
        """The candles the active model is least sure about on ``tf``, closest to a tag's cut first,
        leaving out every (candle, tag) a label already covers. ``scope`` keeps only candles inside
        reviewed ranges ("inside"), outside them ("outside") or both ("all"). The scoring is done
        once per model, timeframe and history and cached; labels are applied on each call."""
        if scope not in ("all", "inside", "outside"):
            raise ValueError("scope is all, inside or outside")
        if tag and tag not in TAGS:
            raise ValueError(f"Unknown tag {tag}")
        bundle = self.active()
        if bundle is None:
            raise ValueError("Set a model active in Models to use the queue")
        tfo = TIMEFRAMES_BY_NAME[tf]
        if tfo.name not in bundle.manifest.get("timeframes", []):
            return {"model_id": bundle.id, "timeframe": tfo.name, "total": 0, "items": [],
                    "note": f"The active model wasn't trained on {tfo.name}"}
        key = (bundle.id, tfo.name, len(m1), m1.index[-1])
        scores = self._queue.get(key)
        if scores is None:
            scores = queue_scores(bundle, m1, tfo)
            self._queue = {k: v for k, v in self._queue.items() if k[:2] != key[:2]}  # one history per model and tf
            self._queue[key] = scores
        df = scores if not tag else scores[scores["tag"] == tag]
        t, tags = df["time_unix"].to_numpy(), df["tag"].to_numpy()
        keep = np.ones(len(df), dtype=bool)
        for lab in self.labels(symbol, tfo.name):
            keep &= ~((tags == lab["tag"]) & (t >= lab["start"]) & (t <= lab["end"]))
        if scope != "all":
            # A reviewed range covers the tags it was reviewed for.
            inside = np.zeros(len(df), dtype=bool)
            for r in self.reviewed(symbol, tfo.name):
                inside |= np.isin(tags, r["tags"]) & (t >= r["start"]) & (t <= r["end"])
            keep &= inside if scope == "inside" else ~inside
        df = df[keep]
        items = [{"id": f"{tfo.name}:{row.tag}:{row.time_unix}", "tag": row.tag, "title": TAGS[row.tag]["title"],
                  "time_unix": int(row.time_unix), "prob": float(row.prob), "cut": float(row.cut),
                  "distance": float(row.distance)} for row in df.head(limit).itertuples(index=False)]
        return {"model_id": bundle.id, "timeframe": tfo.name, "total": int(len(df)), "items": items, "note": None}

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

    def activate(self, model_id: str | None, since_unix: int | None = None) -> None:
        """Set the active model. ``since_unix`` (the latest bar, feed clock) starts its live scorecard."""
        if model_id is None:
            self.store.delete_setting(ACTIVE_KEY)
            return
        if not self._path(model_id).is_file():
            raise ModelFileError("No such model")
        path = self._path(model_id)
        # The file on disk could have changed since it was imported: check it again.
        self._signatures.pop(str(path), None)
        self._check_signature(self._signature(path), trust_file=True)
        self._active = load(path)  # proves it loads before it becomes active
        self.store.set_setting(ACTIVE_KEY, {"id": model_id, "since": _now(), "since_unix": since_unix})

    def scorecard(self, symbol: str, m1: pd.DataFrame | None) -> dict | None:
        """The active model's live results since it was turned on (cached for a minute)."""
        bundle = self.active()
        if bundle is None:
            return None
        st = self.store.get_setting(ACTIVE_KEY) or {}
        if not st.get("since"):  # turned on before Wednesday kept the time: count from now
            last = unix(m1.index[-1]) if m1 is not None and len(m1) else None
            st = {**st, "since": _now(), "since_unix": last}
            self.store.set_setting(ACTIVE_KEY, st)
        reviews = self.reviews(symbol)
        key = (bundle.id, st["since"], len(reviews), m1.index[-1] if m1 is not None and len(m1) else None)
        hit = self._scorecard
        if hit and hit[0] == key and time.monotonic() - hit[1] < SCORECARD_TTL:
            return hit[2]
        card = drift.scorecard(bundle, reviews, m1, st["since"], st.get("since_unix"))
        self._scorecard = (key, time.monotonic(), card)
        return card

    def compare(self, ids: list[str], symbol: str, m1: pd.DataFrame) -> dict:
        """Score 2-3 models on the same window of labels (see :func:`train.score_window`)."""
        if not 2 <= len(ids) <= 3:
            raise ValueError("Pick two or three models to compare")
        bundles = []
        for model_id in ids:
            if not self._path(model_id).is_file():
                raise ModelFileError("No such model")
            bundles.append(load(self._path(model_id)))
        return score_window(bundles, m1, self.by_tf(self.labels(symbol)), self.by_tf(self.reviewed(symbol)),
                            self.detector)

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

    def stage_import(self, data: bytes, symbol: str | None = None, clock: str | None = None) -> dict:
        """Read the manifest only; the pickle waits until :meth:`confirm_import`. ``symbol`` and
        ``clock`` (the running feed's) turn a mismatch with the model's training feed into warnings."""
        manifest = read_manifest(data)
        token = secrets.token_hex(8)
        while len(self._staged) >= 3:
            self._staged.pop(next(iter(self._staged)))
        self._staged[token] = data
        exists = self._path(manifest["id"]).is_file()
        try:
            sig = signing.verify(data, self._trusted())
        except signing.SigningError as exc:
            raise ModelFileError(str(exc)) from exc
        return {"token": token, "manifest": manifest, "exists": exists, "signature": sig,
                "only_signed": self.trust()["only_signed"], "warnings": feed_warnings(manifest, symbol, clock)}

    def confirm_import(self, token: str, trust_file: bool = False) -> dict:
        """Load a staged file. One not signed by a trusted key needs ``trust_file`` (the user's
        "I trust this file"), and none loads when "only signed" is on."""
        data = self._staged.get(token)
        if data is None:
            raise ModelFileError("That upload expired; choose the file again")
        self._check_signature(signing.verify(data, self._trusted()), trust_file)
        del self._staged[token]
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
                "counts": self.counts(symbol), "training": dict(self.training), "runs": self.runs(),
                "models": self.models(), "active": self.active_id}


def feed_warnings(manifest: dict, symbol: str | None, clock: str | None) -> list[str]:
    """Plain-words differences between the feed a model was trained on and the running one."""
    out = []
    trained_on = manifest.get("symbol")
    if symbol and trained_on and trained_on != symbol:
        out.append(f"It was trained on {trained_on}; this feed is {symbol}. Prices and candle shapes can differ.")
    feed = manifest.get("feed") or {}
    if clock and feed.get("clock") and feed["clock"] != clock:
        out.append(f"Its bar times were on the {feed['clock']} clock; this feed's are on {clock}, so its time-of-day "
                   "features will be shifted.")
    elif clock and not feed.get("clock"):
        out.append("The file doesn't say which clock its bar times were on; ask its author if time of day matters.")
    return out


def history_bounds(m1: pd.DataFrame | None) -> dict | None:
    if m1 is None or m1.empty:
        return None
    return {"first_unix": unix(m1.index[0]), "last_unix": unix(m1.index[-1]), "bars": len(m1)}
