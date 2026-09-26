"""Command line entry point: poll M1 data every minute and scan order blocks."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .detectors import DEFAULT_DETECTORS, REGISTRY, DetectorParams, parse_detectors
from .alerts import ALERTS_KEY, AlertManager, AlertSettings, TelegramClient, TelegramError
from .brief import BRIEF_KEY, BriefRunner, BriefSettings
from .journal.service import Journals
from .lab.service import Lab
from .news import CALENDAR_KEY, Calendar, CalendarSettings
from .engine import Runtime
from .settings import SETTINGS_KEY, SOURCES, resolve
from .storage import DEFAULT_DB_URL, Store
from .report import format_scan
from .scanner import ScanConfig
from .timeframes import parse_timeframes

log = logging.getLogger("wednesday")


def load_env_file(path: str | Path) -> None:
    """Load KEY=VALUE lines into os.environ (existing variables win). Missing file is fine."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _env(name: str, cast=str):
    value = os.environ.get(name)
    return cast(value) if value not in (None, "") else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Scan settings resolve as: command line > environment / .env > built-in default.
    # Data source settings add the ones saved from the dashboard (see settings.resolve).
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    known, _ = pre.parse_known_args(argv)
    load_env_file(known.env_file)

    p = argparse.ArgumentParser(prog="wednesday", description=__doc__, parents=[pre])
    p.add_argument("--source", choices=list(SOURCES),
                   help="data source (default: saved in the dashboard, else XAU_SOURCE, else yfinance)")
    p.add_argument("--symbol", help="default per source: GC=F for yfinance, XAUUSD for mt5")
    p.add_argument("--csv", help="M1 CSV path for --source csv (XAU_CSV)")
    p.add_argument("--clock", help="the feed's clock for the quarterly view: UTC, NY+7 (most MT5 brokers), UTC+3 or a "
                                   "time zone like Europe/London (XAU_CLOCK; default: NY+7 for mt5, else UTC)")
    p.add_argument("--db", default=_env("XAU_DB_URL") or DEFAULT_DB_URL,
                   help="database URL for settings and M1 history (XAU_DB_URL); 'none' disables storage")
    p.add_argument("--reset-settings", action="store_true", help="forget data source settings saved from the dashboard")
    p.add_argument("--timeframes", default="4H,1H,30M,15M,5M", help="scanned from high to low")
    p.add_argument("--lookback", type=int, default=200, help="closed candles per timeframe to search")
    p.add_argument("--swing-length", type=int, default=5, help="pivot bars on each side of a swing")
    p.add_argument("--zone", default="body", choices=["wick", "body"],
                   help="OB level covers the candle body (default) or the full range")
    p.add_argument("--mitigation", default="wick", choices=["close", "wick"],
                   help="OB is taken by a wick through the whole body (default) or a close beyond it")
    p.add_argument("--max-sl", type=float, default=_env("XAU_MAX_SL", float) or 3.0,
                   help="max stop distance for OB limit setups, in price units")
    p.add_argument("--detectors", default=_env("XAU_DETECTORS") or ",".join(DEFAULT_DETECTORS),
                   help=f"comma separated: {', '.join(REGISTRY)}")
    p.add_argument("--eq-tolerance", type=float, default=0.1,
                   help="equal highs/lows: max gap as a multiple of ATR(14)")
    p.add_argument("--idm-length", type=int, default=2, help="internal swing bars each side for inducement")
    p.add_argument("--recent-bars", type=int, default=3, help="report levels swept/mitigated within N candles")
    p.add_argument("--delay", type=float, default=2.0, help="seconds after each minute close before polling")
    p.add_argument("--once", action="store_true", help="scan once and exit")
    p.add_argument("--check", action="store_true", help="test the feed connection, print details and exit")
    p.add_argument("--serve", action="store_true", default=_env("XAU_SERVE") == "1",
                   help="run the web dashboard + API alongside the scan loop")
    p.add_argument("--host", default=_env("XAU_HOST") or "127.0.0.1",
                   help="dashboard bind address (0.0.0.0 exposes it to the network - there is no login)")
    p.add_argument("--port", type=int, default=_env("XAU_PORT", int) or 8000)
    p.add_argument("--ui-dir", default=_env("XAU_UI_DIR"), help="built dashboard folder (default web/dist)")
    p.add_argument("--json-out", default=_env("XAU_JSON_OUT"), help="append each scan as a JSON line to this file")
    p.add_argument("--log-file", default=_env("XAU_LOG_FILE"), help="also write logs and scans to this file (rotated)")
    p.add_argument("--digits", type=int, default=2)
    p.add_argument("--mt5-login", type=int, help="MT5_LOGIN")
    p.add_argument("--mt5-password", default=_env("MT5_PASSWORD"), help="prefer MT5_PASSWORD in .env; never stored")
    p.add_argument("--mt5-server", help="MT5_SERVER")
    p.add_argument("--mt5-path", help="path to terminal64.exe (MT5_PATH)")
    p.add_argument("--telegram-chats", action="store_true",
                   help="list chats that messaged your bot (to find TELEGRAM_CHAT_ID) and exit")
    p.add_argument("--telegram-test", action="store_true", help="send a Telegram test message and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def check(feed, buffer, cfg: ScanConfig) -> None:
    for key, value in feed.describe().items():
        print(f"{key:>10}: {value}")
    m1 = buffer.update()
    print(f"{'M1 bars':>10}: {len(m1)} ({m1.index[0]} -> {m1.index[-1]}), need {buffer.max_bars}")
    print(f"{'price':>10}: {feed.last_price() or m1['close'].iloc[-1]}")
    print("OK: feed is working")


def telegram_command(args: argparse.Namespace, client: TelegramClient | None, symbol: str) -> None:
    if client is None:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
    try:
        if args.telegram_chats:
            chats = client.chats()
            if not chats:
                print("No chats yet. Send any message to your bot in Telegram, then run this again.")
            for c in chats:
                print(f"TELEGRAM_CHAT_ID={c['id']}    # {c['type']}: {c['name']}")
        else:
            AlertManager(None, client).send_test(symbol)
            print("Test message sent.")
    except TelegramError as exc:
        raise SystemExit(str(exc)) from exc


def run(args: argparse.Namespace) -> None:
    cfg = ScanConfig(
        timeframes=tuple(parse_timeframes(args.timeframes)),
        lookback=args.lookback,
        detectors=parse_detectors(args.detectors),
        params=DetectorParams(
            swing_length=args.swing_length,
            zone=args.zone,
            mitigation=args.mitigation,
            eq_tolerance=args.eq_tolerance,
            idm_length=args.idm_length,
            max_sl=args.max_sl,
        ),
        recent_bars=args.recent_bars,
    )
    store = None if args.db.lower() == "none" else Store(args.db)
    if store and args.reset_settings:
        store.delete_setting(SETTINGS_KEY)
    data = resolve({
        "source": args.source, "symbol": args.symbol, "csv_path": args.csv,
        "mt5_login": args.mt5_login, "mt5_server": args.mt5_server, "mt5_path": args.mt5_path,
        "clock": args.clock,
    }, store)
    if errors := data.validate():
        raise SystemExit("; ".join(errors))
    json_path = Path(args.json_out) if args.json_out else None

    def publish(result) -> None:
        bias = runtime.active_bias()
        report = format_scan(result, runtime.engine.symbol, args.digits, bias)
        print(report + "\n", flush=True)
        log.debug("scan\n%s", report)
        if json_path:
            record = {"scanned_at": datetime.now(timezone.utc).isoformat(), **result.to_dict(bias)}
            with json_path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")

    token, chat_id = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    client = TelegramClient(token, chat_id) if token else None
    if args.telegram_chats or args.telegram_test:
        telegram_command(args, client, data.resolved_symbol)
        return
    alert_settings = AlertSettings.from_dict(store.get_setting(ALERTS_KEY) if store else None)
    alerts = AlertManager(store, client, alert_settings)
    if alerts.configured:
        log.info("telegram alerts %s for %s, %s OBs", "on" if alert_settings.enabled else "paused",
                 ",".join(alert_settings.timeframes), "/".join(alert_settings.priorities))
    brief = BriefRunner(store, BriefSettings.from_dict(store.get_setting(BRIEF_KEY) if store else None))
    calendar = Calendar(store, CalendarSettings.from_dict(store.get_setting(CALENDAR_KEY) if store else None))
    lab = Lab(store, _env("XAU_MODELS_DIR") or "data/models", cfg.params) if store else None
    journals = Journals(store) if store else None
    runtime = Runtime(cfg, data, store, args.mt5_password, args.delay, publish, alerts, brief, calendar, lab, journals)
    engine = runtime.engine
    feed = engine.feed
    if store:
        log.info("storage: %s", store.describe()["url"])

    if args.check or args.once:
        feed.connect()
        try:
            if args.check:
                check(feed, engine.buffer, cfg)
            else:
                publish(engine.step())
        finally:
            feed.close()
        return

    log.info("feed=%s symbol=%s timeframes=%s lookback=%d (needs %d M1 bars)", feed.name, engine.symbol,
             ",".join(tf.name for tf in cfg.timeframes), cfg.lookback, engine.buffer.max_bars)

    if args.serve:
        import uvicorn

        from .server import create_app

        app = create_app(runtime, ui_dir=args.ui_dir)
        runtime.start()
        log.info("dashboard on http://%s:%d", args.host, args.port)
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        finally:
            runtime.stop()
        return

    try:
        runtime.run_forever()
    except KeyboardInterrupt:
        log.info("stopped")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)  # our own debug output only, not third-party libraries
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)
    if args.log_file:
        # The file always gets DEBUG, which includes every scan table, for troubleshooting on the VPS.
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(args.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    run(args)


if __name__ == "__main__":
    main()
