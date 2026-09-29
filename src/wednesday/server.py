"""HTTP API + static React dashboard.

The scan engine runs on a background thread; request handlers only read its
snapshots, so they never touch the data feed (MT5) directly.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import Auth, install as install_auth
from .alerts import ALERTS_KEY, AlertSettings, TelegramError, telegram_client
from .bias import TradeBias
from .brief import BRIEF_KEY, BriefError, BriefSettings
from .llm_usage import BudgetExceeded
from .news import CALENDAR_KEY, CalendarSettings
from .drawings import clean_drawing, drawing_payload
from .engine import RECONNECT_ATTEMPTS, Engine, Runtime
from .lab.api import lab_router
from .journal.api import journal_router
from .features import catalog as feature_catalog
from .history import older_candles
from . import plugin_install
from .plugins import PLUGIN_API, features, load_new_plugins, load_plugins
from .quarters import NEW_YORK, quarter_blocks, quarters_payload, utc_to_feed
from .distribution import distribution as move_distribution, tables as distribution_tables_of
from .mt5_terminals import find_terminals
from .secret_store import get_secret, update_secrets
from .sizing import RiskSettings, pip_size
from .settings import SETTINGS_KEY, DataSettings, catalog, source_availability
from .terms import Terms
from .timeframes import TIMEFRAMES_BY_NAME, resample_ohlcv

# The Windows desktop build (PyInstaller) unpacks the dashboard next to the code, under sys._MEIPASS.
DEFAULT_UI_DIR = (Path(sys._MEIPASS) if getattr(sys, "frozen", False)
                  else Path(__file__).resolve().parents[2]) / "web" / "dist"


def _unix(ts: pd.Timestamp) -> int:
    # Feed times are naive (broker server time); the chart shows them as-is by treating them as UTC.
    return int(ts.timestamp())


def _candle_rows(candles: pd.DataFrame) -> list[dict]:
    return [
        {"time": _unix(t), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
        for t, r in zip(candles.index, candles.itertuples(index=False))
    ]


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


log = logging.getLogger(__name__)

DIST_TTL = 6 * 3600  # seconds the stored history's periods are kept; the live buffer's are fresh
DIST_BARS = 2_000_000  # stored M1 bars read for the distribution: about five years
DIST_DAYS = 3000  # daily bars asked of the feed, for lookbacks past the M1 history


def clock_offset(clock: str) -> int:
    """Seconds the feed clock is ahead of UTC right now (the chart's times are feed-clock unix)."""
    now = pd.Timestamp.now(tz="UTC")
    return round((utc_to_feed(now, clock) - now.tz_localize(None)).total_seconds() / 60) * 60


def create_app(target: Engine | Runtime, source: str = "", ui_dir: str | Path | None = None,
               clock: str = "UTC", auth: Auth | None = None) -> FastAPI:
    """Serve a fixed :class:`Engine`, or a :class:`Runtime` whose data source the dashboard can change.
    ``auth`` turns on the login (see :mod:`wednesday.auth`); without it the dashboard is open."""
    app = FastAPI(title="Wednesday", docs_url="/api/docs", openapi_url="/api/openapi.json")
    runtime = target if isinstance(target, Runtime) else None
    cfg = target.cfg
    install_auth(app, auth or Auth())

    @app.get("/api/health")
    def health() -> dict:
        """Liveness for proxies and monitors; the only route open without a login."""
        return {"ok": True, "version": __version__}

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
                "conn": st.conn,
                "tick_seconds": engine.tick_seconds if 0 < engine.tick_seconds < 60 else 0,
                "pip": pip_size(engine.symbol, st.result.price if st.result else None, st.spec),
                "clock_offset": clock_offset(current_clock()),
                "poll": engine.poll,  # False with --no-poll: one scan, then nothing new
                "app_version": __version__,
                "config": cfg.to_dict(),
            }

    terms = Terms(runtime.store if runtime else target.store)

    @app.get("/api/terms")
    def get_terms() -> dict:
        """The disclaimer and risk agreement, and whether it was accepted."""
        return terms.status()

    @app.post("/api/terms/accept")
    def accept_terms() -> dict:
        terms.accept()
        return terms.status()

    def with_sizes(scan_dict: dict) -> dict:
        """Each setup's lot size for its stop, when Settings > Risk is on."""
        sizer = runtime.sizer() if runtime else None
        scan_dict["sizing"] = sizer.to_dict() if sizer else None
        for setups in scan_dict["setups"].values():
            for s in setups:
                s["size"] = sizer.size(s["meta"].get("risk"), s["risk"]) if sizer else None
        return scan_dict

    def risk_payload() -> dict:
        engine = current()
        with engine.state.lock:
            spec = engine.state.spec
        sizer = runtime.sizer() if runtime else None
        return {"editable": runtime is not None, "settings": runtime.risk.to_dict() if runtime else None,
                "mt5": spec, "sizer": sizer.to_dict() if sizer else None}

    @app.get("/api/risk")
    def get_risk() -> dict:
        """Settings > Risk, what the terminal reports (MT5), and the sizing in use."""
        return risk_payload()

    @app.put("/api/risk")
    def put_risk(body: dict = Body(...)) -> dict:
        if not runtime:
            raise HTTPException(409, "Risk settings need the server running with --serve")
        try:
            new = RiskSettings.from_dict({**runtime.risk.to_dict(), **body})
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        runtime.set_risk(new)
        return risk_payload()

    @app.get("/api/status")
    def get_status() -> dict:
        return status()

    @app.get("/api/scan")
    def get_scan() -> dict:
        _, result, _ = current().snapshot()
        return {**status(), "scan": with_sizes(result.to_dict(trade_bias())) if result else None}

    @app.get("/api/tick")
    def get_tick() -> dict:
        """The live price between scans and the forming M1 candle, small enough to poll every second."""
        st = current().state
        with st.lock:
            live, version, tick = st.live, st.version, st.tick
        bar = live["bar"] if live else None
        return {
            "version": version,  # a new scan: refetch /api/scan
            "tick": tick,
            "price": live["price"] if live else None,
            "time": _unix(live["time"]) if live else None,
            "bar": {"time": _unix(bar["time"]), **{k: bar[k] for k in ("open", "high", "low", "close")}} if bar else None,
        }

    # ---- drawings: per source and symbol, so they follow a data source switch ----
    def drawing_store():
        store = runtime.store if runtime else target.store
        if store is None:
            raise HTTPException(409, "Drawings need a database")
        return store

    @app.get("/api/drawings")
    def get_drawings() -> dict:
        rows = drawing_store().drawings(current_source(), current().symbol)
        return {"source": current_source(), "symbol": current().symbol, "drawings": [drawing_payload(r) for r in rows]}

    @app.put("/api/drawings/{drawing_id}")
    def put_drawing(drawing_id: str, body: dict = Body(...)) -> dict:
        try:
            row = clean_drawing(drawing_id, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        drawing_store().drawing_put({**row, "source": current_source(), "symbol": current().symbol})
        return row

    @app.delete("/api/drawings/{drawing_id}")
    def delete_drawing(drawing_id: str) -> dict:
        if not drawing_store().drawing_delete(drawing_id, current_source(), current().symbol):
            raise HTTPException(404, "No such drawing")
        return {"deleted": drawing_id}

    @app.get("/api/candles")
    def get_candles(tf: str = Query("1H"), limit: int = Query(200, ge=10, le=2000),
                    before: int | None = Query(None)) -> dict:
        """The latest candles with their levels; with ``before`` (unix), older candles only, for scrolling back."""
        timeframe = TIMEFRAMES_BY_NAME.get(tf.upper())
        if timeframe is None:
            raise HTTPException(404, f"unknown timeframe {tf!r}")
        if before is not None:
            try:
                older, more = older_candles(current(), timeframe, before, limit)
            except RuntimeError as exc:  # MT5 closed or not connected
                raise HTTPException(503, str(exc)) from exc
            return {"timeframe": timeframe.name, "candles": _candle_rows(older), "has_more": more}
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
            "candles": _candle_rows(candles),
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

    # ---- move distribution: stored history's periods (slow to read, kept a while) + the live buffer's ----
    dist_cache: dict = {}

    def distribution_tables():
        engine = current()
        _, _, m1 = engine.snapshot()
        if m1 is None or m1.empty:
            raise HTTPException(503, "no data yet")
        clock, key = current_clock(), (current_source(), engine.symbol, current_clock())
        base = dist_cache.get(key)
        if base is None or time.monotonic() - base["at"] > DIST_TTL:
            store = runtime.store if runtime else target.store
            stored = store.load_bars(current_source(), engine.symbol, DIST_BARS) if store and engine.feed.persist else None
            union = pd.concat([stored[stored.index < m1.index[0]], m1]) if stored is not None and len(stored) else m1
            try:
                d1 = engine.call(lambda feed: feed.daily_history(clock, DIST_DAYS), timeout=60)
            except Exception:  # noqa: BLE001 - the M1 history alone still works
                log.debug("no daily history", exc_info=True)
                d1 = None
            base = {"at": time.monotonic(), "blocks": quarter_blocks(union, clock), "d1": d1}
            dist_cache.clear()
            dist_cache[key] = base
        # Closed history from the cache, the recent days (from the second one the buffer holds whole) fresh.
        fresh = quarter_blocks(m1, clock)
        days = fresh["week"]
        cutoff = days[1].day if len(days) > 1 else (days[0].day if days else None)
        merged = {row: [b for b in base["blocks"][row] if cutoff is None or b.day < cutoff] +
                  [b for b in fresh[row] if cutoff is not None and b.day >= cutoff] for row in fresh}
        return distribution_tables_of(merged, base["d1"])

    @app.get("/api/distribution")
    def get_distribution(period: str = Query("day"), measure: str = Query("change"), lookback: str = Query("1y"),
                         weekday: bool = Query(False), session: bool = Query(False),
                         bins: int = Query(40, ge=10, le=80)) -> dict:
        """How unusual the current period's move is against past closed periods (see distribution.py)."""
        try:
            return move_distribution(distribution_tables(), period, measure, lookback, weekday, session, bins)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/clock/feed")
    def ny_to_feed(ny: str = Query(..., description="New York wall time, e.g. 2026-09-24T09:30")) -> dict:
        """A New York wall time as the chart's feed-clock unix seconds (for go to date)."""
        try:
            wall = pd.Timestamp(ny)
        except ValueError as exc:
            raise HTTPException(422, "ny is a date and time like 2026-09-24T09:30") from exc
        if wall.tzinfo is not None:
            raise HTTPException(422, "ny is a New York wall time, without an offset")
        # A time the spring change skips moves on to 03:00; one the autumn change repeats takes the first.
        aware = wall.tz_localize(NEW_YORK, ambiguous=True, nonexistent="shift_forward")
        feed = utc_to_feed(aware, current_clock())
        return {"ny": wall.isoformat(), "feed": feed.isoformat(), "feed_unix": int(feed.timestamp())}

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
        update_secrets(body, ["anthropic_api_key", "openai_api_key"])
        if runtime.store:
            runtime.store.set_setting(BRIEF_KEY, new.to_dict())
        brief.settings = new
        return {"editable": True, **brief.status()}

    @app.post("/api/brief/generate", status_code=202)
    def generate_brief(body: dict | None = Body(None)) -> dict:
        """Start a brief. Over the monthly LLM budget it needs ``{"confirm_over_budget": true}``."""
        brief = need_brief()
        try:
            brief.start(brief_context(), confirmed=bool((body or {}).get("confirm_over_budget")))
        except BudgetExceeded as exc:
            raise HTTPException(409, str(exc), headers={"X-Over-Budget": "1"}) from exc
        except BriefError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"editable": True, **brief.status()}

    def usage_log():
        if not runtime:
            raise HTTPException(409, "LLM usage is only kept when the server runs with --serve")
        return runtime.usage

    @app.get("/api/llm/usage")
    def llm_usage() -> dict:
        """This month's LLM spend by feature and model, the last 30 days, the costliest calls,
        the budget and the price table."""
        return usage_log().report()

    @app.put("/api/llm/budget")
    def put_llm_budget(body: dict = Body(...)) -> dict:
        log = usage_log()
        try:
            log.set_budget(body.get("monthly_usd"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"The budget is an amount in US dollars: {exc}") from exc
        return log.report()

    @app.put("/api/llm/prices")
    def put_llm_prices(body: dict = Body(...)) -> dict:
        """The price table: {model: {input, output, cache_read?, cache_write?}} in USD per million tokens."""
        log = usage_log()
        try:
            log.set_prices(body.get("models") or {})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return log.report()

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
                "conn": engine.state.conn,
                "attempt": engine.state.attempt,
                "max_attempts": None if engine.feed.retry_connect else RECONNECT_ATTEMPTS,
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
            # Only whether a password is known and where it's kept; never the password itself.
            "mt5_password": runtime.mt5_password_for(runtime.settings)[1] if runtime else None,
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
        body = dict(body)
        password = str(body.pop("mt5_password", None) or "")
        forget = bool(body.pop("forget_mt5_password", False))
        try:
            new = DataSettings.from_dict({**DataSettings().to_dict(), **body})
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"Invalid settings: {exc}") from exc
        errors = new.validate()
        ok, reason = source_availability(new.source) if not errors else (True, None)
        if not ok:
            errors.append(reason)
        if not errors and (err := new.terminal_error()):
            errors.append(err)
        if password and new.mt5_login is None:
            errors.append("Enter the MT5 login that goes with the password")
        if errors:
            raise HTTPException(422, "; ".join(errors))
        if forget:
            runtime.forget_mt5_password(new.mt5_login, new.mt5_server)
        if password:
            runtime.set_mt5_password(new.mt5_login, new.mt5_server, password)
        if runtime.store:
            runtime.store.set_setting(SETTINGS_KEY, new.to_dict())
        runtime.apply(new)
        return settings_payload()

    @app.post("/api/settings/reconnect")
    def reconnect() -> dict:
        if runtime is None:
            raise HTTPException(409, "The feed can only be restarted when the server runs with --serve")
        runtime.reconnect()
        return settings_payload()

    @app.get("/api/mt5/terminals")
    def mt5_terminals() -> dict:
        """MT5 terminals installed on this PC, to pick one in Settings."""
        return {"terminals": find_terminals()}

    def alerts_payload() -> dict:
        alerts = runtime.alerts if runtime else None
        client = alerts.client if alerts else None
        store = runtime.store if runtime else None
        return {
            "editable": alerts is not None,
            "token_set": bool(client and client.token),
            # Where the token comes from ("saved", "session" or "env"); never the token itself.
            "token_source": get_secret("telegram_bot_token")[1],
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
        token_changed = update_secrets(body, ["telegram_bot_token"])
        chat_changed = new.resolved_chat_id != runtime.alerts.settings.resolved_chat_id
        if runtime.store:
            runtime.store.set_setting(ALERTS_KEY, new.to_dict())
        runtime.alerts.settings = new
        if token_changed or chat_changed:
            runtime.alerts.client = telegram_client(new)
        return alerts_payload()

    @app.get("/api/alerts/chats")
    def alert_chats() -> dict:
        """Chats that messaged the bot recently, to pick the chat id without a terminal."""
        if not runtime or runtime.alerts is None:
            raise HTTPException(409, "Alerts are only available when the server runs with --serve")
        client = runtime.alerts.client
        if client is None:
            raise HTTPException(409, "Save the bot token first")
        try:
            return {"chats": client.chats()}
        except TelegramError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/alerts/test")
    def test_alert() -> dict:
        if not runtime or runtime.alerts is None:
            raise HTTPException(409, "Alerts are only available when the server runs with --serve")
        try:
            runtime.alerts.send_test(current().symbol)
        except TelegramError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True}

    app.include_router(lab_router(lambda: runtime.lab if runtime else None, current, current_source, current_clock))
    app.include_router(journal_router(lambda: runtime.journals if runtime else None, current,
                                      lambda: features(app.state.plugins)))

    # Plugins mount their routes before the dashboard's catch-all static mount below.
    plugins = load_plugins(app, runtime) if runtime else []
    app.state.plugins = plugins

    @app.get("/api/plugins")
    def get_plugins() -> dict:
        """Installed plugins and the features they provide (the dashboard unlocks screens from this)."""
        enabled = features(plugins)
        return {"api": PLUGIN_API, "plugins": [p.to_dict() for p in plugins], "features": enabled,
                "catalog": feature_catalog(enabled)}

    def licence_provider():
        return runtime.hooks.licence if runtime else None

    @app.get("/api/licence")
    def get_licence() -> dict:
        """The licence, when a plugin handles licences; otherwise just that none does."""
        provider = licence_provider()
        return {"available": False} if provider is None else {"available": True, **provider.status()}

    def download_plugin(key: str):
        """No plugin handles licences yet: a licence key from the licence server fetches Wednesday EE."""
        if runtime is None:
            raise HTTPException(409, "The licence needs the server running with --serve")
        if not key.upper().startswith("WEDK-"):
            raise HTTPException(409, "Wednesday EE isn't installed. Enter a licence key (WEDK-...) "
                                     "and Wednesday downloads it.")
        try:
            manifest = plugin_install.install(key, runtime.store)
        except plugin_install.InstallError as exc:
            raise HTTPException(422, str(exc)) from exc
        for info in load_new_plugins(app, runtime, plugins):
            if not info.loaded:
                raise HTTPException(500, f"Wednesday EE {manifest['version']} was downloaded but didn't load: "
                                         f"{info.error}")
        provider = licence_provider()
        if provider is None:
            raise HTTPException(500, "Wednesday EE was downloaded; restart Wednesday to finish")
        return provider

    @app.put("/api/licence")
    def put_licence(body: dict = Body(...)) -> dict:
        key = str(body.get("key") or "").strip()
        if not key:
            raise HTTPException(422, "Paste a licence key")
        provider = licence_provider() or download_plugin(key)
        try:
            return {"available": True, **provider.activate(key)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/licence")
    def delete_licence() -> dict:
        provider = licence_provider()
        if provider is None:
            raise HTTPException(409, "No plugin handles licences")
        return {"available": True, **provider.clear()}

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
