"""Command line entry point: poll M1 data every minute and scan order blocks."""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .feeds import M1Buffer, build_feed
from .report import format_scan
from .scanner import ScanConfig, scan
from .timeframes import parse_timeframes

log = logging.getLogger("xau_screener")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="xau-screener", description=__doc__)
    p.add_argument("--source", default="mt5", choices=["mt5", "yfinance", "csv", "synthetic"])
    p.add_argument("--symbol", default=None, help="XAUUSD for mt5 (default), GC=F for yfinance")
    p.add_argument("--csv", help="M1 CSV path for --source csv")
    p.add_argument("--timeframes", default="4H,1H,30M,15M,5M", help="scanned from high to low")
    p.add_argument("--lookback", type=int, default=200, help="closed candles per timeframe to search")
    p.add_argument("--swing-length", type=int, default=5, help="pivot bars on each side of a swing")
    p.add_argument("--zone", default="wick", choices=["wick", "body"])
    p.add_argument("--mitigation", default="close", choices=["close", "wick"])
    p.add_argument("--delay", type=float, default=2.0, help="seconds after each minute close before polling")
    p.add_argument("--once", action="store_true", help="scan once and exit")
    p.add_argument("--json-out", help="append each scan as a JSON line to this file")
    p.add_argument("--digits", type=int, default=2)
    p.add_argument("--mt5-login", type=int)
    p.add_argument("--mt5-password")
    p.add_argument("--mt5-server")
    p.add_argument("--mt5-path")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def sleep_until_next_minute(delay: float) -> None:
    now = time.time()
    time.sleep(60 - (now % 60) + delay)


def run(args: argparse.Namespace) -> None:
    cfg = ScanConfig(
        timeframes=tuple(parse_timeframes(args.timeframes)),
        lookback=args.lookback,
        swing_length=args.swing_length,
        zone=args.zone,
        mitigation=args.mitigation,
    )
    mt5_kwargs = {}
    if args.source == "mt5":
        mt5_kwargs = {"login": args.mt5_login, "password": args.mt5_password,
                      "server": args.mt5_server, "path": args.mt5_path}
    feed = build_feed(args.source, args.symbol, args.csv, **mt5_kwargs)
    symbol = args.symbol or ("GC=F" if args.source == "yfinance" else "XAUUSD")
    buffer = M1Buffer(feed, max_bars=cfg.required_m1_bars())
    json_path = Path(args.json_out) if args.json_out else None

    feed.connect()
    log.info("feed=%s symbol=%s timeframes=%s lookback=%d (needs %d M1 bars)", feed.name, symbol,
             ",".join(tf.name for tf in cfg.timeframes), cfg.lookback, buffer.max_bars)
    try:
        while True:
            try:
                m1 = buffer.update()
                result = scan(m1, cfg, feed.last_price())
                print(format_scan(result, symbol, args.digits), flush=True)
                print(flush=True)
                if json_path:
                    record = {"scanned_at": datetime.now(timezone.utc).isoformat(), **result.to_dict()}
                    with json_path.open("a") as fh:
                        fh.write(json.dumps(record) + "\n")
            except Exception:
                if args.once:
                    raise
                log.exception("scan failed; retrying next minute")
            if args.once:
                break
            sleep_until_next_minute(args.delay)
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        feed.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    run(args)


if __name__ == "__main__":
    main()
