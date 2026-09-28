"""HTTP routes of the Lab (``/api/lab/...``)."""

from __future__ import annotations

import io
import json
from typing import Callable

import pandas as pd
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

from ..detectors import build_detectors
from ..structure import Context
from ..timeframes import TIMEFRAMES_BY_NAME, resample_ohlcv
from .dataset import LADDER, minute_dataset, ts, unix
from .bars import HISTORY_BARS_MAX, MAX_IMPORT_BYTES, BarsFileError, import_bars
from .model import MAX_FILE_BYTES, ModelFileError
from .service import Lab, LabelsFileError, history_bounds, ml_available
from .tags import TAGS, tag_of
from .train import TrainParams, predict_blocks

DETECTOR_CONTEXT = 150  # candles before the window the detectors see, so levels near its left edge are found


def lab_router(get_lab: Callable[[], Lab | None], get_engine: Callable, get_source: Callable[[], str],
               get_clock: Callable[[], str] = lambda: "UTC") -> APIRouter:
    r = APIRouter(prefix="/api/lab")
    resampled: dict = {}

    def lab() -> Lab:
        found = get_lab()
        if found is None:
            raise HTTPException(409, "The Lab needs the server running with --serve and a database")
        return found

    def need_ml() -> None:
        ok, reason = ml_available()
        if not ok:
            raise HTTPException(409, reason)

    def symbol() -> str:
        return get_engine().symbol

    def live_m1() -> pd.DataFrame | None:
        return get_engine().snapshot()[2]

    def history() -> pd.DataFrame | None:
        return lab().history(get_source(), symbol(), live_m1())

    def candles_of(m1: pd.DataFrame, tf_name: str) -> pd.DataFrame:
        key = (get_source(), symbol(), tf_name)
        stamp = (len(m1), m1.index[-1])
        hit = resampled.get(key)
        if hit is None or hit[0] != stamp:
            hit = (stamp, resample_ohlcv(m1, TIMEFRAMES_BY_NAME[tf_name]))
            resampled[key] = hit
        return hit[1]

    def timeframe(name: str):
        tf = TIMEFRAMES_BY_NAME.get(name.upper())
        if tf is None:
            raise HTTPException(404, f"unknown timeframe {name!r}")
        return tf

    @r.get("")
    def status() -> dict:
        found = get_lab()
        if found is None:
            return {"editable": False, "available": ml_available()[0], "reason": "The Lab needs the server running with --serve and a database"}
        m1 = history()
        return {"editable": True, "symbol": symbol(), "history": history_bounds(m1), **found.status(symbol())}

    # ---- Data: more M1 history (import, MT5 backfill, coverage) ----------------------------
    def stream() -> tuple[str, str] | None:
        """Where bars of the running source are stored; None for demo data (never stored)."""
        return get_engine().buffer.key

    def data_status() -> dict:
        found = lab()
        engine = get_engine()
        key = stream()
        n, first, last = found.store.bar_bounds(*key) if key else (0, None, None)
        return {
            "source": get_source(), "symbol": symbol(), "clock": get_clock(),
            "can_import": key is not None, "can_backfill": key is not None and engine.feed.native_history,
            "stored": {"bars": n, "first_unix": first, "last_unix": last},
            "days": found.store.bar_days(*key) if key else [],
            "history_bars": found.history_bars, "history_bars_max": HISTORY_BARS_MAX,
            "backfill": dict(found.backfill.state),
        }

    @r.get("/data")
    def get_data() -> dict:
        """Stored M1 coverage per day, the Lab's history size, and the backfill's progress."""
        return data_status()

    @r.put("/data")
    def put_data(body: dict = Body(...)) -> dict:
        try:
            lab().set_history_bars(int(body.get("history_bars", 0)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return data_status()

    @r.post("/data/import")
    async def stage_bars(request: Request) -> dict:
        """Upload an M1 file (raw body); returns a preview. Nothing is stored until the confirm call."""
        if stream() is None:
            raise HTTPException(409, "Demo data isn't stored: switch to Yahoo Finance, MT5 or CSV first")
        data = await request.body()
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "The file is larger than 500 MB")
        try:
            return lab().stage_bars(data)
        except BarsFileError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.post("/data/import/{token}")
    def confirm_bars(token: str, body: dict = Body(...)) -> dict:
        """Store a staged file's bars, moved from the file's clock (``clock``) to the feed's."""
        key = stream()
        if key is None:
            raise HTTPException(409, "Demo data isn't stored: switch to Yahoo Finance, MT5 or CSV first")
        found = lab()
        try:
            parsed = found.take_bars(token)
            result = import_bars(found.store, key, parsed, str(body.get("clock") or get_clock()), get_clock())
        except BarsFileError as exc:
            raise HTTPException(422, str(exc)) from exc
        found.forget_history()
        return {"imported": result, **data_status()}

    @r.post("/data/backfill")
    def start_backfill(body: dict = Body(...)) -> dict:
        """Pull older M1 bars from the MT5 terminal back to ``start`` (YYYY-MM-DD, feed clock)."""
        try:
            start = pd.Timestamp(str(body.get("start")))
        except ValueError:
            start = pd.NaT
        if pd.isna(start) or start >= pd.Timestamp.now():
            raise HTTPException(422, "start must be a past date like 2024-01-01")
        found = lab()
        try:
            found.backfill.start(get_engine(), found.store, start, on_done=found.forget_history)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return data_status()

    @r.delete("/data/backfill")
    def cancel_backfill() -> dict:
        lab().backfill.cancel()
        return data_status()

    @r.get("/candles")
    def candles(tf: str = Query("5M"), end: int | None = Query(None), limit: int = Query(300, ge=20, le=2000),
                model: bool = Query(False)) -> dict:
        """A window of closed candles ending at ``end`` (unix, feed clock; default: latest) with the
        labels, reviewed ranges, detector suggestions and, with ``model``, the active model's blocks."""
        tfo = timeframe(tf)
        m1 = history()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        allc = candles_of(m1, tfo.name)
        upto = allc if end is None else allc[allc.index <= ts(end)]
        window = upto.tail(limit)
        if window.empty:
            return {"timeframe": tfo.name, "candles": [], "labels": [], "reviewed": [], "suggestions": [],
                    "predictions": [], "has_more": False, "history": history_bounds(m1)}
        start_u, end_u = unix(window.index[0]), unix(window.index[-1])

        ctx_candles = upto.tail(limit + DETECTOR_CONTEXT)
        suggestions = []
        found = lab()
        if len(ctx_candles) > 10:
            ctx = Context(ctx_candles)
            for det in build_detectors(("ob", "liquidity", "idm"), found.detector):
                for lv in det.detect(ctx):
                    tag = tag_of(lv)
                    if tag is None or lv.time < window.index[0]:
                        continue
                    suggestions.append({"id": f"{tfo.name}:{tag}:{unix(lv.time)}", "tag": tag, "time_unix": unix(lv.time),
                                        "top": lv.top, "bottom": lv.bottom, "label": lv.label,
                                        "priority": lv.meta.get("priority")})

        predictions = []
        if model:
            need_ml()
            bundle = found.active()
            if bundle is not None:
                m1_upto = m1[m1.index < window.index[-1] + tfo.delta]
                predictions = predict_blocks(bundle, m1_upto, tfo, limit=len(window))
                verdicts = {(v["tag"], v["time"]): v["verdict"] for v in found.reviews(symbol(), tfo.name)
                            if v["model_id"] == bundle.id}
                for p in predictions:
                    p["model_id"] = bundle.id
                    p["verdict"] = verdicts.get((p["tag"], p["time_unix"]))

        return {
            "timeframe": tfo.name,
            "candles": [{"time": unix(t), "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                        for t, c in zip(window.index, window.itertuples(index=False))],
            "labels": found.labels(symbol(), tfo.name, start_u, end_u),
            "reviewed": found.reviewed(symbol(), tfo.name, start_u, end_u),
            "suggestions": suggestions,
            "predictions": predictions,
            "has_more": len(upto) > len(window),
            "is_latest": end is None or len(upto) == len(allc),
            "history": history_bounds(m1),
        }

    @r.get("/queue")
    def queue(tf: str = Query("5M"), tag: str | None = Query(None), scope: str = Query("all"),
              limit: int = Query(200, ge=1, le=1000)) -> dict:
        """Review queue: candles across the history where the active model is least sure (a tag's
        probability closest to its cut), without the ones a label already covers."""
        need_ml()
        tfo = timeframe(tf)
        m1 = history()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        try:
            return lab().queue(symbol(), m1, tfo.name, tag or None, scope, limit)
        except ValueError as exc:
            raise HTTPException(409 if "active" in str(exc) else 422, str(exc)) from exc

    @r.get("/backtest")
    def backtest(tf: str = Query("15M"), start: int | None = Query(None, alias="from"),
                 end: int | None = Query(None, alias="to"), rr: float = Query(2.0),
                 horizon: int = Query(72, description="hours"), filter: str = Query("all"),
                 min_win: float = Query(0.5, ge=0, le=1), format: str = Query("json")):
        """Every detector order block of the history traded with the limit plan: trades,
        aggregates and splits; ``format=csv`` downloads the trades."""
        tfo = timeframe(tf)
        m1 = history()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        try:
            result = lab().backtest(symbol(), m1, tfo.name, get_clock(), start, end, rr, horizon, filter, min_win)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if format == "csv":
            from .backtest import trades_csv

            name = f"wednesday-backtest-{symbol()}-{tfo.name}-{filter}.csv"
            return Response(trades_csv(result["trades"]), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})
        return result

    @r.post("/labels")
    def add_label(body: dict = Body(...)) -> dict:
        origin = body.get("origin") if body.get("origin") in ("manual", "detector") else "manual"
        try:
            return lab().add_label(symbol(), body, origin)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.delete("/labels/{label_id}")
    def delete_label(label_id: str) -> dict:
        return {"deleted": lab().delete("labels", label_id)}

    @r.post("/batch")
    def batch(body: dict = Body(...)) -> dict:
        """Add and delete labels and reviewed ranges in one call (bulk edits, undo / redo)."""
        try:
            return lab().batch(symbol(), body)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.get("/labels/export")
    def export_labels() -> Response:
        """Every label and reviewed range of the symbol, as a JSON file to back up or pass on."""
        doc = lab().export_labels(symbol())
        name = f"wednesday-labels-{symbol().replace('=', '')}-{doc['exported_at'][:10]}.json"
        return Response(json.dumps(doc, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.post("/labels/import")
    async def stage_labels(request: Request) -> dict:
        """Upload a labels file (raw body); returns what would change. Nothing is written until the confirm call."""
        data = await request.body()
        try:
            return lab().stage_labels(data, symbol())
        except LabelsFileError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.post("/labels/import/{token}")
    def confirm_labels(token: str, body: dict = Body(default={})) -> dict:
        """Merge a staged labels file into this symbol (``map_symbol`` for a file of another one)."""
        try:
            return {"imported": lab().confirm_labels(token, symbol(), bool(body.get("map_symbol"))), **status()}
        except LabelsFileError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.post("/reviewed")
    def add_reviewed(body: dict = Body(...)) -> dict:
        try:
            return lab().add_reviewed(symbol(), body)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.delete("/reviewed/{row_id}")
    def delete_reviewed(row_id: str) -> dict:
        return {"deleted": lab().delete("reviewed", row_id)}

    @r.get("/dataset")
    def dataset(fmt: str = Query("parquet", alias="format"), days: int = Query(30, ge=1, le=400)) -> Response:
        """Per-minute table: M1 OHLCV, the forming candle of every timeframe, one label column per timeframe and tag."""
        m1 = history()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        found = lab()
        m1 = m1[m1.index >= m1.index[-1] - pd.Timedelta(days=days)]
        frame = minute_dataset(m1, found.by_tf(found.labels(symbol())), found.by_tf(found.reviewed(symbol())),
                               list(TAGS), LADDER)
        name = f"wednesday-{symbol().replace('=', '')}-{days}d"
        if fmt == "csv":
            return Response(frame.to_csv(), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        need_ml()
        buf = io.BytesIO()
        frame.to_parquet(buf)
        return Response(buf.getvalue(), media_type="application/vnd.apache.parquet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.parquet"'})

    @r.post("/train", status_code=202)
    def train(body: dict = Body(...)) -> dict:
        need_ml()
        params = TrainParams.from_dict(body)
        if errors := params.validate():
            raise HTTPException(422, "; ".join(errors))
        m1 = history()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        try:
            lab().start_training(m1, symbol(), params, feed={"source": get_source(), "clock": get_clock()})
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return status()

    @r.delete("/train")
    def cancel_training() -> dict:
        """Stop the running training at its next stage; nothing is saved."""
        if not lab().cancel_training():
            raise HTTPException(409, "Nothing is training")
        return status()

    @r.put("/active")
    def set_active(body: dict = Body(...)) -> dict:
        need_ml()
        m1 = live_m1()
        try:
            lab().activate(body.get("id") or None, unix(m1.index[-1]) if m1 is not None and len(m1) else None)
        except ModelFileError as exc:
            raise HTTPException(422, str(exc)) from exc
        return status()

    @r.get("/scorecard")
    def scorecard() -> dict:
        """The active model's live results since it was turned on, with quiet drift warnings."""
        found = get_lab()
        if found is None or not ml_available()[0]:
            return {"scorecard": None}
        return {"scorecard": found.scorecard(symbol(), history())}

    @r.post("/compare")
    def compare(body: dict = Body(...)) -> dict:
        """Score two or three models on the same window: labels after the newest one's training data."""
        need_ml()
        m1 = history()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        try:
            return lab().compare([str(i) for i in body.get("ids") or []], symbol(), m1)
        except ModelFileError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.delete("/models/{model_id}")
    def delete_model(model_id: str) -> dict:
        try:
            if not lab().delete_model(model_id):
                raise HTTPException(404, "No such model")
        except ModelFileError as exc:
            raise HTTPException(422, str(exc)) from exc
        return status()

    @r.get("/models/{model_id}/file")
    def model_file(model_id: str) -> Response:
        try:
            data = lab().export(model_id)
        except ModelFileError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="wednesday-model-{model_id}.zip"'})

    @r.post("/models/import")
    async def import_model(request: Request) -> dict:
        """Upload a model file (raw body). Only its manifest is read; loading waits for the confirm call."""
        data = await request.body()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "The file is larger than 200 MB")
        try:
            return lab().stage_import(data, symbol(), get_clock())
        except ModelFileError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.post("/models/import/{token}")
    def confirm_import(token: str, body: dict = Body(default={})) -> dict:
        """Load a staged model file; ``trust`` is the user's "I trust this file" for one that
        isn't signed by a trusted key."""
        need_ml()
        try:
            manifest = lab().confirm_import(token, bool(body.get("trust")))
        except ModelFileError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"imported": manifest, **status()}

    @r.get("/trust")
    def get_trust() -> dict:
        """Public keys whose signatures are trusted, and whether only signed models load."""
        return lab().trust()

    @r.put("/trust")
    def put_trust(body: dict = Body(...)) -> dict:
        try:
            return lab().set_trust(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.get("/predictions")
    def predictions(tf: str = Query("5M"), limit: int = Query(200, ge=10, le=2000),
                    threshold: float = Query(0, ge=0, le=0.99)) -> dict:
        """The active model's blocks on the live chart (the screener's ML layer). A threshold
        of 0 uses each tag's cut from training."""
        found = get_lab()
        if found is None or not ml_available()[0]:
            return {"model": None, "blocks": []}
        bundle = found.active()
        m1 = live_m1()
        if bundle is None or m1 is None:
            return {"model": bundle.manifest if bundle else None, "blocks": []}
        tfo = timeframe(tf)
        blocks = predict_blocks(bundle, m1, tfo, limit=limit, threshold=threshold or None)
        verdicts = {(v["tag"], v["time"]): v for v in found.reviews(symbol(), tfo.name) if v["model_id"] == bundle.id}
        for b in blocks:
            v = verdicts.get((b["tag"], b["time_unix"]))
            b.update(model_id=bundle.id, verdict=v["verdict"] if v else None, outcome=v["outcome"] if v else None)
        return {"model": {"id": bundle.id, "name": bundle.manifest["name"]}, "blocks": blocks}

    @r.post("/reviews")
    def review(body: dict = Body(...)) -> dict:
        try:
            return lab().add_review(symbol(), body, history())
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.get("/feedback")
    def feedback() -> Response:
        """Reviews with the candle's features and both rewards, as CSV."""
        m1 = history()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        frame = lab().feedback_frame(symbol(), m1)
        return Response(frame.to_csv(index=False), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="wednesday-feedback.csv"'})

    return r
