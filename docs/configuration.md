# Configuration

Settings come from, strongest first: command line flags, what the dashboard
saved (data source and alerts), `.env`, then defaults. Copy `.env.example` to
`.env` (`make setup` does this) and fill in what you need. `.env` is gitignored;
never commit real credentials.

## Make targets (macOS / Linux)

| Target | What it does |
| --- | --- |
| `make setup` | First run: check cargo, uv + Node, create `.env`, install everything, build the dashboard |
| `make serve` | The Rust server: scan engine + dashboard on `http://HOST:PORT`, data in this folder |
| `make serve-py` | The Python server from source (`wednesday --serve`), the reference implementation |
| `make dev` | The Rust server on synthetic data (a random walk, for development) + Vite dev server with hot reload on http://localhost:5173 |
| `make tauri` | The [desktop app](desktop.md) from this checkout |
| `make scan` | One scan in the console (Python) |
| `make check` | Test the data feed connection (Python) |
| `make test` / `make typecheck` | Python tests / dashboard type check |
| `make telegram-chats` / `make telegram-test` | Find `TELEGRAM_CHAT_ID` / send a test message |
| `make clean` | Remove build output and caches (keeps `data/` and `.env`) |

Variables: `HOST`, `PORT`, and `ARGS` for extra CLI flags, e.g.
`make serve-py PORT=9000 ARGS="--lookback 300"`. On Windows, run the `uv run
wednesday ...` commands directly (see [deploy-windows.md](deploy-windows.md)).

The Rust server (`make serve`, the desktop app) takes `--host`, `--port`, `--insecure`,
`--login`, `--create-user NAME`,
`--home` (the data folder), `--db`, `--ui-dir` and `--source`; its scan settings come from
`.env` and the dashboard (see [Development](development.md#the-rust-server-96)).

## Command line options

These are Python's (`wednesday`, from source). `uv run wednesday --help` lists everything. The useful ones:

| Option | Default | Meaning |
| --- | --- | --- |
| `--source` | `tradingview` | `tradingview`, or `synthetic` for development and tests (`XAU_SOURCE`). A saved or env `mt5`, `yfinance` or `csv` (from older versions) falls back to `tradingview` with a warning |
| `--symbol` | per source | Symbol to scan (`XAU_SYMBOL`) |
| `--clock` | per source | The feed's clock for the quarterly view: `UTC`, `NY+7`, `UTC+3`, `Europe/London` (`XAU_CLOCK`; default `UTC`) |
| `--timeframes` | `4H,1H,30M,15M,5M` | Timeframes to scan (always processed high to low) |
| `--lookback` | `200` | Closed candles per timeframe searched for levels |
| `--swing-length` | `5` | Bars on each side needed to confirm a swing high/low |
| `--zone` | `body` | OB level covers the body (`body`) or the full candle range (`wick`) |
| `--mitigation` | `wick` | OB is taken by a wick through the whole body (`wick`) or a close beyond it (`close`) |
| `--max-sl` | `3.0` | Max stop distance for OB limit setups, in price units (`XAU_MAX_SL`) |
| `--detectors` | `ob,liquidity,idm` | Detectors to run (`XAU_DETECTORS`) |
| `--eq-tolerance` | `0.1` | Equal highs/lows: max gap as a multiple of ATR(14) |
| `--idm-length` | `2` | Internal swing bars each side for inducement |
| `--recent-bars` | `3` | Report levels swept/mitigated within this many candles |
| `--serve` | off | Also run the web dashboard + API (`XAU_SERVE=1`) |
| `--no-poll` | off | With `--serve`: scan once at start, then fetch nothing (no minute scans, no live prices); for working on the dashboard (the status shows *Paused*) |
| `--host` / `--port` | `127.0.0.1` / `8000` | Dashboard address (`XAU_HOST`, `XAU_PORT`); a non-loopback host turns the login on |
| `--login` | off | Ask for a login on `127.0.0.1` too (`XAU_LOGIN=1`); see [Access](dashboard.md#access) |
| `--insecure` | off | Serve a non-loopback host without a login (not recommended) |
| `--create-user NAME` | - | Create the login's account, or set a new password for it, then exit |
| `--db` | `sqlite:///data/xau.db` | Storage URL (`XAU_DB_URL`); `none` disables it |
| `--reset-settings` | off | Forget the data source saved from the dashboard |
| `--once` | off | Scan once and exit instead of looping every minute |
| `--check` | off | Test the feed connection (account, symbol, bars loaded) and exit |
| `--log-file` | - | Rotating log file, includes every scan table (`XAU_LOG_FILE`) |
| `--json-out` | - | Append each scan as a JSON line (`XAU_JSON_OUT`) |
| `--delay` | `2` | Seconds after the minute closes before polling |
| `--telegram-chats` / `--telegram-test` | - | See [alerts.md](alerts.md) |

The 4H timeframe with `--lookback 200` needs about 48k M1 bars. They are loaded
once at startup (from the database when stored); after that only the last 30
bars are fetched each minute.

## Environment variables

| Variable | Used for |
| --- | --- |
| `XAU_SOURCE`, `XAU_SYMBOL` | Data source and symbol |
| `XAU_CLOCK` | The feed's clock ([Feed clock](data-sources.md#feed-clock)) |
| `XAU_TICK` | Live price interval in seconds, 0 = off ([Live price](data-sources.md#live-price)) |
| `XAU_DB_URL` | Storage URL |
| `XAU_MAX_SL`, `XAU_DETECTORS` | Strategy settings |
| `XAU_SERVE`, `XAU_HOST`, `XAU_PORT` | Dashboard |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts; fallback when not set in Settings |
| `XAU_LOG_FILE`, `XAU_JSON_OUT` | Logs and JSON lines output |
