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
from .engine import Engine
from .feeds import build_feed
from .report import format_scan
from .scanner import ScanConfig
from .timeframes import parse_timeframes

log = logging.getLogger("xau_screener")


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
    # Settings resolve as: command line > environment / .env > built-in default.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    known, _ = pre.parse_known_args(argv)
    load_env_file(known.env_file)

    p = argparse.ArgumentParser(prog="xau-screener", description=__doc__, parents=[pre])
    p.add_argument("--source", default=_env("XAU_SOURCE") or "mt5",
                   choices=["mt5", "yfinance", "csv", "synthetic"])
    p.add_argument("--symbol", default=_env("XAU_SYMBOL"), help="XAUUSD for mt5 (default), GC=F for yfinance")
    p.add_argument("--csv", help="M1 CSV path for --source csv")
    p.add_argument("--timeframes", default="4H,1H,30M,15M,5M", help="scanned from high to low")
    p.add_argument("--lookback", type=int, default=200, help="closed candles per timeframe to search")
    p.add_argument("--swing-length", type=int, default=5, help="pivot bars on each side of a swing")
    p.add_argument("--zone", default="wick", choices=["wick", "body"])
    p.add_argument("--mitigation", default="close", choices=["close", "wick"])
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
    p.add_argument("--mt5-login", type=int, default=_env("MT5_LOGIN", int))
    p.add_argument("--mt5-password", default=_env("MT5_PASSWORD"), help="prefer MT5_PASSWORD in .env")
    p.add_argument("--mt5-server", default=_env("MT5_SERVER"))
    p.add_argument("--mt5-path", default=_env("MT5_PATH"), help="path to terminal64.exe")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def check(feed, buffer, cfg: ScanConfig) -> None:
    for key, value in feed.describe().items():
        print(f"{key:>10}: {value}")
    m1 = buffer.update()
    print(f"{'M1 bars':>10}: {len(m1)} ({m1.index[0]} -> {m1.index[-1]}), need {buffer.max_bars}")
    print(f"{'price':>10}: {feed.last_price() or m1['close'].iloc[-1]}")
    print("OK: feed is working")


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
        ),
        recent_bars=args.recent_bars,
    )
    mt5_kwargs = {}
    if args.source == "mt5":
        mt5_kwargs = {"login": args.mt5_login, "password": args.mt5_password,
                      "server": args.mt5_server, "path": args.mt5_path}
    feed = build_feed(args.source, args.symbol, args.csv, **mt5_kwargs)
    symbol = args.symbol or ("GC=F" if args.source == "yfinance" else "XAUUSD")
    engine = Engine(feed, cfg, symbol)
    json_path = Path(args.json_out) if args.json_out else None

    def publish(result) -> None:
        report = format_scan(result, symbol, args.digits)
        print(report + "\n", flush=True)
        log.debug("scan\n%s", report)
        if json_path:
            record = {"scanned_at": datetime.now(timezone.utc).isoformat(), **result.to_dict()}
            with json_path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")

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

    log.info("feed=%s symbol=%s timeframes=%s lookback=%d (needs %d M1 bars)", feed.name, symbol,
             ",".join(tf.name for tf in cfg.timeframes), cfg.lookback, engine.buffer.max_bars)

    if args.serve:
        import uvicorn

        from .server import create_app

        app = create_app(engine, source=args.source, ui_dir=args.ui_dir)
        engine.start(args.delay, publish)
        log.info("dashboard on http://%s:%d", args.host, args.port)
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        finally:
            engine.stop()
        return

    try:
        engine.run_forever(args.delay, publish)
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
