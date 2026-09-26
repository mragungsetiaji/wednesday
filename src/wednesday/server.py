"""HTTP API + static React dashboard.

The scan engine runs on a background thread; request handlers only read its
snapshots, so they never touch the data feed (MT5) directly.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .alerts import ALERTS_KEY, AlertSettings, TelegramError
from .bias import TradeBias
from .brief import BRIEF_KEY, BriefError, BriefSettings
from .news import CALENDAR_KEY, CalendarSettings
from .engine import Engine, Runtime
from .lab.api import lab_router
from .plugins import PLUGIN_API, features, load_plugins
from .quarters import quarters_payload, utc_to_feed
from .settings import SETTINGS_KEY, DataSettings, catalog, source_availability
from .timeframes import TIMEFRAMES_BY_NAME, resample_ohlcv

DEFAULT_UI_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"


def _unix(ts: pd.Timestamp) -> int:
    # Feed times are naive (broker server time); the chart shows them as-is by treating them as UTC.
    return int(ts.timestamp())


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def create_app(target: Engine | Runtime, source: str = "", ui_dir: str | Path | None = None,
               clock: str = "UTC") -> FastAPI:
    """Serve a fixed :class:`Engine`, or a :class:`Runtime` whose data source the dashboard can change."""
    app = FastAPI(title="Wednesday", docs_url="/api/docs", openapi_url="/api/openapi.json")
    runtime = target if isinstance(target, Runtime) else None
    cfg = target.cfg

    def current() -> Engine:
        # Looked up per request: applying new settings swaps the engine.
        return runtime.engine if runtime else target

    def current_source() -> str:
        return runtime.settings.source if runtime else source

    def current_clock() -> str:
        return runtime.settings.resolved_clock if runtime else clock

    quarters_cache: dict = {}

    def trade_bias() -> TradeBias | None:
        return runtime.active_bias() if runtime else None

    def bias_payload() -> dict | None:
        return runtime.bias.to_dict() if runtime and runtime.bias else None

    def status() -> dict:
        engine = current()
        st = engine.state
        with st.lock:
            return {
                "symbol": engine.symbol,
                "source": current_source(),
                "clock": current_clock(),
                "trade_bias": bias_payload(),
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
        _, result, _ = current().snapshot()
        return {**status(), "scan": result.to_dict(trade_bias()) if result else None}

    @app.get("/api/candles")
    def get_candles(tf: str = Query("1H"), limit: int = Query(200, ge=10, le=2000)) -> dict:
        timeframe = TIMEFRAMES_BY_NAME.get(tf.upper())
        if timeframe is None:
            raise HTTPException(404, f"unknown timeframe {tf!r}")
        _, result, m1 = current().snapshot()
        if result is None or m1 is None:
            raise HTTPException(503, "no data yet")
        # Include the still-forming candle so the chart matches the live price.
        candles = resample_ohlcv(m1, timeframe, drop_incomplete=False).tail(limit)
        tf_result = next((r for r in result.results if r.timeframe.name == timeframe.name), None)
        levels = [lv.to_dict() for s in tf_result.sets.values() for lv in s.active] if tf_result else []
        swings = [sw.to_dict() for sw in tf_result.swings] if tf_result else []
        return {
            "timeframe": timeframe.name,
            "price": result.price,
            "candles": [
                {"time": _unix(t), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                for t, r in zip(candles.index, candles.itertuples(index=False))
            ],
            "levels": levels,
            "swings": swings,  # confirmed swing points labelled HH / LH / HL / LL
        }

    @app.get("/api/quarters")
    def get_quarters() -> dict:
        """Quarterly theory blocks (week, session, 90 minutes) over the loaded M1 history."""
        engine = current()
        version, _, m1 = engine.snapshot()
        if m1 is None:
            raise HTTPException(503, "no data yet")
        key = (id(engine), version, current_clock())
        if quarters_cache.get("key") != key:
            quarters_cache.update(key=key, value=quarters_payload(m1, current_clock()))
        return quarters_cache["value"]

    @app.get("/api/bias")
    def get_bias() -> dict:
        return {"bias": bias_payload()}

    @app.put("/api/bias")
    def put_bias(body: dict = Body(...)) -> dict:
        """Set the trade bias ({direction, note, expiry}); a null direction clears it."""
        if runtime is None:
            raise HTTPException(409, "The bias can only be set when the server runs with --serve")
        if not body.get("direction"):
            runtime.set_bias(None)
            return {"bias": None}
        new = TradeBias(str(body["direction"]), str(body.get("note") or ""), str(body.get("expiry") or "day"))
        if errors := new.validate():
            raise HTTPException(422, "; ".join(errors))
        runtime.set_bias(new.stamped())
        return {"bias": bias_payload()}

    def brief_context() -> dict:
        engine = current()
        _, result, _ = engine.snapshot()
        ctx = {"Symbol": engine.symbol, "Now (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}
        if result is not None:
            ctx["Price"] = f"{result.price:.2f}"
            for r in result.results:
                if r.bias:
                    ctx[f"{r.timeframe.name} structure"] = f"{r.bias.direction} {r.bias.event} at {r.bias.level:.2f}"
        bias = trade_bias()
        ctx["Trader's current bias"] = f"{bias.direction}" + (f" ({bias.note})" if bias and bias.note else "") if bias else "not set"
        return ctx

    def need_brief():
        if not runtime or runtime.brief is None:
            raise HTTPException(409, "The brief is only available when the server runs with --serve")
        return runtime.brief

    @app.get("/api/brief")
    def get_brief() -> dict:
        if not runtime or runtime.brief is None:
            return {"editable": False}
        return {"editable": True, **runtime.brief.status()}

    @app.put("/api/brief")
    def put_brief(body: dict = Body(...)) -> dict:
        brief = need_brief()
        try:
            new = BriefSettings.from_dict(body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"Invalid brief settings: {exc}") from exc
        if errors := new.validate():
            raise HTTPException(422, "; ".join(errors))
        if runtime.store:
            runtime.store.set_setting(BRIEF_KEY, new.to_dict())
        brief.settings = new
        return {"editable": True, **brief.status()}

    @app.post("/api/brief/generate", status_code=202)
    def generate_brief() -> dict:
        brief = need_brief()
        try:
            brief.start(brief_context())
        except BriefError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"editable": True, **brief.status()}

    def with_chart_times(snap: dict) -> dict:
        """Add each event's time on the chart's (feed clock) axis, so news lines up with the candles."""
        clock = current_clock()
        for key in ("events", "week"):
            snap[key] = [{**e, "chart_time_unix": _unix(utc_to_feed(pd.Timestamp(e["time"]), clock))} for e in snap[key]]
        return snap

    @app.get("/api/calendar")
    def get_calendar() -> dict:
        """Upcoming news matching the calendar settings (fetched in the background, at most hourly)."""
        if not runtime or runtime.calendar is None:
            return {"editable": False, "events": [], "week": []}
        return {"editable": True, **with_chart_times(runtime.calendar.snapshot())}

    @app.put("/api/calendar")
    def put_calendar(body: dict = Body(...)) -> dict:
        if not runtime or runtime.calendar is None:
            raise HTTPException(409, "The calendar is only available when the server runs with --serve")
        new = CalendarSettings.from_dict(body)
        if errors := new.validate():
            raise HTTPException(422, "; ".join(errors))
        if runtime.store:
            runtime.store.set_setting(CALENDAR_KEY, new.to_dict())
        runtime.calendar.settings = new
        return {"editable": True, **with_chart_times(runtime.calendar.snapshot())}

    def settings_payload() -> dict:
        engine = current()
        _, _, m1 = engine.snapshot()
        with engine.state.lock:
            running = {
                "source": current_source(),
                "symbol": engine.symbol,
                "version": engine.state.version,
                "scanned_at": _iso(engine.state.scanned_at),
                "error": engine.state.error,
                "bars_loaded": 0 if m1 is None else len(m1),
                "bars_needed": engine.buffer.max_bars,
                "first_bar": _iso(m1.index[0]) if m1 is not None and len(m1) else None,
                "last_bar": _iso(m1.index[-1]) if m1 is not None and len(m1) else None,
            }
        store = runtime.store if runtime else engine.store
        return {
            "editable": runtime is not None,
            "settings": runtime.settings.to_dict() if runtime else None,
            "sources": catalog(),
            "mt5_password_set": bool(runtime and runtime.mt5_password),
            "running": running,
            "storage": {**store.describe(), "series": store.bar_stats()} if store else None,
        }

    @app.get("/api/settings")
    def get_settings() -> dict:
        return settings_payload()

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)) -> dict:
        if runtime is None:
            raise HTTPException(409, "Settings can only be changed when the server runs with --serve")
        try:
            new = DataSettings.from_dict({**DataSettings().to_dict(), **body})
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"Invalid settings: {exc}") from exc
        errors = new.validate()
        ok, reason = source_availability(new.source) if not errors else (True, None)
        if not ok:
            errors.append(reason)
        if errors:
            raise HTTPException(422, "; ".join(errors))
        if runtime.store:
            runtime.store.set_setting(SETTINGS_KEY, new.to_dict())
        runtime.apply(new)
        return settings_payload()

    def alerts_payload() -> dict:
        alerts = runtime.alerts if runtime else None
        client = alerts.client if alerts else None
        store = runtime.store if runtime else None
        return {
            "editable": alerts is not None,
            "token_set": bool(client and client.token),
            "chat_id_set": bool(client and client.chat_id),
            "configured": bool(alerts and alerts.configured),
            "settings": alerts.settings.to_dict() if alerts else None,
            "last_error": alerts.last_error if alerts else None,
            "recent": store.recent_alerts(20) if store else [],
        }

    @app.get("/api/alerts")
    def get_alerts() -> dict:
        return alerts_payload()

    @app.put("/api/alerts")
    def put_alerts(body: dict = Body(...)) -> dict:
        if not runtime or runtime.alerts is None:
            raise HTTPException(409, "Alerts can only be changed when the server runs with --serve")
        new = AlertSettings.from_dict(body)
        if errors := new.validate():
            raise HTTPException(422, "; ".join(errors))
        if runtime.store:
            runtime.store.set_setting(ALERTS_KEY, new.to_dict())
        runtime.alerts.settings = new
        return alerts_payload()

    @app.post("/api/alerts/test")
    def test_alert() -> dict:
        if not runtime or runtime.alerts is None:
            raise HTTPException(409, "Alerts are only available when the server runs with --serve")
        try:
            runtime.alerts.send_test(current().symbol)
        except TelegramError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True}

    app.include_router(lab_router(lambda: runtime.lab if runtime else None, current, current_source))

    # Plugins mount their routes before the dashboard's catch-all static mount below.
    plugins = load_plugins(app, runtime) if runtime else []
    app.state.plugins = plugins

    @app.get("/api/plugins")
    def get_plugins() -> dict:
        """Installed plugins and the features they provide (the dashboard unlocks screens from this)."""
        return {"api": PLUGIN_API, "plugins": [p.to_dict() for p in plugins], "features": features(plugins)}

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
