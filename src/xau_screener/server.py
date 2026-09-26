"""HTTP API + static React dashboard.

The scan engine runs on a background thread; request handlers only read its
snapshots, so they never touch the data feed (MT5) directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .timeframes import TIMEFRAMES_BY_NAME, resample_ohlcv

DEFAULT_UI_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"


def _unix(ts: pd.Timestamp) -> int:
    # Feed times are naive (broker server time); the chart shows them as-is by treating them as UTC.
    return int(ts.timestamp())


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def create_app(engine: Engine, source: str = "", ui_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="XAU SMC Screener", docs_url="/api/docs", openapi_url="/api/openapi.json")
    cfg = engine.cfg

    def status() -> dict:
        st = engine.state
        with st.lock:
            return {
                "symbol": engine.symbol,
                "source": source,
                "version": st.version,
                "scanned_at": _iso(st.scanned_at),
                "error": st.error,
                "error_at": _iso(st.error_at),
                "config": cfg.to_dict(),
            }

    @app.get("/api/status")
    def get_status() -> dict:
        return status()

    @app.get("/api/scan")
    def get_scan() -> dict:
        _, result, _ = engine.snapshot()
        return {**status(), "scan": result.to_dict() if result else None}

    @app.get("/api/candles")
    def get_candles(tf: str = Query("1H"), limit: int = Query(200, ge=10, le=2000)) -> dict:
        timeframe = TIMEFRAMES_BY_NAME.get(tf.upper())
        if timeframe is None:
            raise HTTPException(404, f"unknown timeframe {tf!r}")
        _, result, m1 = engine.snapshot()
        if result is None or m1 is None:
            raise HTTPException(503, "no data yet")
        # Include the still-forming candle so the chart matches the live price.
        candles = resample_ohlcv(m1, timeframe, drop_incomplete=False).tail(limit)
        tf_result = next((r for r in result.results if r.timeframe.name == timeframe.name), None)
        levels = [lv.to_dict() for s in tf_result.sets.values() for lv in s.active] if tf_result else []
        return {
            "timeframe": timeframe.name,
            "price": result.price,
            "candles": [
                {"time": _unix(t), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                for t, r in zip(candles.index, candles.itertuples(index=False))
            ],
            "levels": levels,
        }

    ui = Path(ui_dir or os.environ.get("XAU_UI_DIR") or DEFAULT_UI_DIR)
    if (ui / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return (
                "<h1>XAU screener API is running</h1>"
                f"<p>Dashboard not built yet (looked in <code>{ui}</code>). "
                "Run <code>npm install && npm run build</code> in <code>web/</code>, "
                "or use the dev server: <code>npm run dev</code>.</p>"
                '<p>API docs: <a href="/api/docs">/api/docs</a></p>'
            )

    return app
