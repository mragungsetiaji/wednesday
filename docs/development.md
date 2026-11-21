# Development

Run from source (macOS, Linux or Windows):

```bash
git clone https://github.com/mragungsetiaji/wednesday.git
cd wednesday
make setup       # checks cargo, uv + Node, creates .env, installs everything, builds the dashboard
make serve       # the Rust server: dashboard on http://127.0.0.1:8000 (TradingView data)
make tauri       # the desktop app from this checkout
make             # list every target
```

The server is Rust (`server-rs/`). Python stays in the repository as the reference: it
writes the golden files the Rust tests read (`make test`), and runs from source as
`make serve-py` (`wednesday --serve`), `make scan` and `make check`. Extra flags go
through `ARGS`: `make serve ARGS="--source synthetic"` for the Rust server,
`make serve-py PORT=9000 ARGS="--lookback 300"` for Python's. On Windows without make,
see [Windows and VPS](deploy-windows.md).

```bash
make install     # Python deps (uv) + dashboard and TradingView helper deps (npm)
make test        # pytest (and the golden files)
make test-tauri  # the Rust tests: the shell and the server
make typecheck   # TypeScript
make dev         # the Rust server on synthetic data (random walk) + Vite hot reload on http://localhost:5173
```

Needs Rust 1.77.2 or newer ([rustup](https://rustup.rs)), [uv](https://docs.astral.sh/uv/)
and Node 20+. Without make:

```bash
uv sync && uv run pytest
cd server-rs && cargo run -- --source synthetic --home ..   # API on :8000
cd web && npm install && npm run dev                         # Vite on :5173, /api proxied to :8000
```

## Project layout

```
src/wednesday/
  timeframes.py   timeframe list + M1 -> higher timeframe resampling
  structure.py    shared swings / break of structure / ATR (computed once per timeframe)
  levels.py       Level: the common output of every detector
  detectors/      order blocks, liquidity (BSL/SSL, EQH/EQL), inducement + registry
  feeds.py        TradingView and synthetic (development) feeds + rolling M1 buffer
  scanner.py      multi-timeframe scan, nearest level above/below, limit setups
  quarters.py     quarterly theory blocks (weekday, session, 90m) in New York time + green stats
  report.py       console table
  engine.py       scan loop shared by the console and the web server
  server.py       FastAPI: /api/scan, /api/candles, /api/tick and /api/stream (live price, pushed), /api/quarters, /api/bias, /api/calendar (+ /reactions, /import), /api/settings, /api/alerts, serves the dashboard
  settings.py     data source settings, source catalog, precedence rules
  alerts.py       Telegram alerts when price enters an order block
  bias.py         the trader's bias, its expiry, and the risk on / off label per setup
  news.py         economic calendar (ForexFactory weekly feed) for the risk-time card; every week kept in calendar_events
  news_stats.py   gold's move after past releases per event type (M1, feed clock), calendar CSV import
  storage.py      SQLAlchemy store: settings, M1 history, alert log, journals (SQLite / PostgreSQL)
  history.py      older candles for the chart as it scrolls left (memory, then stored M1)
  journal/        MT5 accounts: HTML report import, gain and drawdown rebuilt from M1 bars, /api/journals; sample.py builds a made-up portfolio for tests and the docs screenshot
  plugins.py      loads plugins (entry point group wednesday.plugins): routes, hooks, jobs
  cli.py          command line entry point
web/              React + Vite + TypeScript dashboard (lightweight-charts)
src-tauri/        the Tauri desktop shell (#92), with the Rust server in its process
server-rs/        the server: scan engine, API and dashboard build (#96)
scripts/          Windows VPS: auto-restart wrapper + Task Scheduler installer
docs/             these docs
tests/
```

## The API contract

The dashboard and the Tauri shell only use the server through `/api`.
`tests/test_api_contract.py` keeps that API in `tests/api_contract.json`: every
route, every request body and query, and the shape (keys and types, not values)
of every GET response, on synthetic data. A change to the API fails the test
with a list of what changed. When the change is meant, update the dashboard to
match, then rewrite the file and review its diff:

```bash
UPDATE_API_CONTRACT=1 uv run pytest tests/test_api_contract.py
```

It is also what a replacement server must answer the same way (#96).
The scan's own output is kept the same way in `tests/golden/` (see
[Reference output](detectors.md#reference-output)).

## The Rust server (#96)

The backend moved to Rust a route at a time; `server-rs/` is now the server. It is an
[axum](https://github.com/tokio-rs/axum) server that runs the scan engine, answers every route
the dashboard calls and serves the dashboard build (`web/dist`, or `XAU_UI_DIR`; `ui.rs` does what
Starlette's `StaticFiles(html=True)` did). An unknown `/api` route is FastAPI's 404,
`{"detail": "Not Found"}`. The desktop app runs it in its own process; there is no Python in the
installer.

The Rust crates form one Cargo workspace (`Cargo.toml` at the root): one `Cargo.lock`, one
`target/` folder, one version (`[workspace.package]`, set by `scripts/set_version.py`). Debug
builds keep only line tables for our code and no debug info for dependencies, and
`.cargo/config.toml` limits builds to two jobs (CI sets `CARGO_BUILD_JOBS`).
The integration tests are one binary, `server-rs/tests/it/` (`main.rs` lists the modules; a test that
sets environment variables holds `env_lock()`): `cargo test -p wednesday-rs --test it <filter>`.

```bash
cd server-rs && cargo run -- [--host 127.0.0.1] [--port 8000] [--home DIR] [--db URL] [--ui-dir DIR] [--source synthetic]
```

`--home` is the data folder (its `.env`, and `data/xau.db` by default; `make serve` and `make dev`
use the checkout). Like `wednesday --serve`, it refuses a host that isn't loopback unless
`--insecure`: the dashboard has no login. `XAU_HOST` and `XAU_PORT` work as for Python.

For comparing the two, `--backend <port>` puts it back in front of a Python server
(`make serve-rs`): it answers the routes in `PORTED` and `ENGINE_ROUTES` and passes every other
request to Python, streaming the answer back. Each answer then carries
`x-wednesday-server: rust` or `python`.

- **The database.** `server-rs/src/store.rs` opens the same database as Python
  (`XAU_DB_URL` from the environment or the home folder's `.env`, else
  `sqlite:///data/xau.db` in the home folder), on SQLite or PostgreSQL. It makes the
  tables from `server-rs/schema.json`, which `tests/test_rust_schema.py` generates from
  `storage.py`, and stores JSON as Python's `json.dumps` writes it. When Rust can't open
  the database (or `XAU_DB_URL=none`), Python serves those routes.
- **No login.** The dashboard runs on the trader's machine; ported routes refuse
  cross-site writes as `same_site.py` does.
- **Drawings and alerts** run with the engine (step 9e). `server-rs/src/drawings/` has the
  drawings routes and the drawing alerts (`checker.rs`, checked after each scan and, for
  intrabar alerts, on each tick); `server-rs/src/alerts/` has the order block alerts, the
  Telegram client and `/api/alerts`. The bot token is read and saved in the OS credential store
  where Python's `keyring` keeps it (`alerts/secrets.rs`: Keychain on macOS, Credential Manager
  on Windows; on Linux only `.env` or the session), so either server finds the other's token.
  `tests/golden/alerts/` and `tests/golden/drawings/` hold the reference output
  (`tests/test_alerts_golden.py`, `tests/test_drawings_golden.py`). `watch_alerts` in `lib.rs`
  runs the checks; Python's plugins still hear `on_alert`, from the `alert_log` rows Rust wrote.
- **The journal** is in `server-rs/src/journal/`: the MT5 report import (`report.rs`, its own
  HTML tokenizer copying Python's `HTMLParser`), the stats (`stats.rs`, numpy and pandas
  arithmetic copied to the last bit) and the routes (`service.rs`). `tests/golden/reports/` and
  `tests/golden/journal/` hold the reference output both must give (`tests/test_report_golden.py`,
  `tests/test_journal_golden.py`; the stats files match Python 3.11's `sum()`). Writing an entry
  (`POST .../entries`, with the engine) freezes the market of the moment (`journal/market.rs`:
  the last scan, the active bias, the price and the coming news; `tests/golden/market/`).
  A second journal needs the `journal.multi` feature, read from the plugin host's registry
  (`server-rs/src/plugins/`, see `docs/plugins.md`).
- **The engine** runs in Rust (`server-rs/src/engine/`): the feed (`feed.rs`: the TradingView
  helper, synthetic data), the M1 buffer stored in `m1_bars`, the scan after each minute close,
  the live price and the forming candle. It answers `/api/status`, `/api/scan`, `/api/tick`,
  `/api/stream`, `/api/settings`, the charts (`charts.rs`: `/api/candles`, `/api/replay`,
  `/api/replay/range`, `/api/clock/feed`), the analysis views (`analysis.rs`: `/api/sessions`,
  `/api/quarters`, `/api/distribution`) and other markets (`markets.rs`: `/api/markets/candles`,
  `/api/markets/tick`), all in `ENGINE_ROUTES`. `tests/golden/distribution/` and
  `tests/test_markets_parity.py` check them against Python. The Python server *follows* it: with
  `WEDNESDAY_FOLLOW=http://127.0.0.1:<port>` its feed is `feeds.FollowFeed`, which reads the bars,
  the scan price and the ticks from `GET /api/engine/m1` and `/api/tick` instead of connecting to
  TradingView, so its plugins see what Rust scanned. It sends no alerts itself. A settings
  change saved through Rust tells Python to follow (`POST /api/settings/reconnect`).
  From a checkout: `make serve-rs ARGS=--engine` (Rust on 8000), then
  `WEDNESDAY_FOLLOW=http://127.0.0.1:8000 make serve-py PORT=8001`; `conformance.rs` checks the follower's
  answers against Rust's. Python on its own (`make serve-py`) runs its own feed.
- **The calendar** is ported in `server-rs/src/calendar/`: the settings, the weekly
  ForexFactory fetch (`Calendar`, at most hourly, retried ten minutes after a failure, the last
  good week kept in `calendar_cache`), the history (`calendar_events`), the past reactions on the
  engine's M1 bars and feed clock (`reactions.rs`) and the CSV import (`import.rs`, Python's `csv`
  reader and the `pd.Timestamp` forms calendars use). `tests/golden/calendar/` holds the reference
  output (`tests/test_calendar_golden.py`); `calendar_fetch.rs` runs the fetch against a local
  fake source. With the engine, Rust answers `/api/calendar`, its reactions and imports, and
  fetches whenever the week is stale (Python fetched only when the dashboard asked); without
  it, Rust only checks and saves the settings and Python answers.
- **Terms, bias, snapshots** (`terms.rs`, `bias.rs`, `snapshots.rs`). The disclaimer is built
  into the binary; `TERMS_VERSION` must equal `terms.py`'s (a unit test checks). The bias is
  only ever what the trader sent; the Python follower reads it from the database.

To add or change a route:

1. Add its handler in `server-rs/src/lib.rs` and the route to `PORTED` (or `ENGINE_ROUTES`).
2. Its answer must have the shape `tests/api_contract.json` gives it; the Rust tests
   check every GET route listed there. A route that scans must also match
   `tests/golden/`. Python's server is the reference: change it the same way.
3. Add its requests to `server-rs/tests/it/conformance.rs`, which sends each one to the
   real Python server and to Rust and compares the answers (status and JSON).

```bash
make test-tauri                 # the shell's and the Rust server's tests
make serve-py PORT=8001         # the Python server ...
make serve-rs PORT=8000         # ... and the Rust one in front, on http://127.0.0.1:8000
```

`server-rs/tests/it/storage.rs` checks the store against `storage.py`: the same tables,
and what one writes the other reads. It runs on SQLite, and on PostgreSQL when
`initdb` works or `WEDNESDAY_TEST_POSTGRES` names a server where it may create
databases (`postgresql://postgres@127.0.0.1:5432/postgres`); otherwise it says
PostgreSQL was skipped.

The scan exists twice (`server-rs/src/scan/` and `src/wednesday/`: resampling, structure,
the detectors, setups, session levels and quarters) and both give the same output on
`tests/golden/`. A change to one goes into the other.

## How a scan flows

Two threads (`engine.py`). The **feed thread** owns the feed: a feed's connection
is only used from the thread that opened it. It does light work only: live
ticks, the M1 fetch at each minute close, and short tasks queued with
`Engine.call` (scrolling back, plugins). Every task over
0.2 s is logged, since the live price waits for it. The **scan worker**
(`ScanWorker`) takes the fetched bars and does steps 2 to 5. If it falls behind,
only the newest fetch is scanned. Anything after a scan that reads the feed must go
through `engine.call`.

1. `M1Buffer` loads stored bars, fetches the newest ones from the feed and saves them (feed thread).
2. `resample_ohlcv` builds each timeframe, dropping the candle still forming.
3. Per timeframe, a shared `Context` computes structure once; each detector returns `Level`s.
4. `scanner` picks the nearest level above/below per detector and ranks the limit setups.
5. `Runtime` runs the Telegram alert check, then the console report or the API picks up the result.

### Chart performance

Charts apply a scan's candles with `series.update` when only the last bars
changed, and `setData` only when history did (`perf.ts`, `planUpdate`). Panes on
the same timeframe share one `/api/candles` request per scan, and a chart out of
view skips live ticks until it shows again. Animations that run all the time
must animate only `opacity` and `transform`: an animated `box-shadow` on the Live
dot repainted every frame and was over half of the idle CPU. **Alt+Shift+P** (or
`?perf` in the address) shows frames per second and the loads, updates and ticks
per second. With 4 panes on the synthetic feed, Chrome's task time over 75 s went from
21.7 s to 9.9 s with these changes.

Things that follow the mouse stay out of React state (#91): the crosshair's
candle goes to a small store that only the OHLC legend reads, and a drawing being
dragged goes through `dragBus` (`drawings.ts`), so every chart redraws it on its
canvas and React only hears the drop. The overlay's **renders/s** counts chart
re-renders: about one per live tick, none from hovering, panning or dragging. With
4 charts, hovering went from about 400 renders/s to under 10, and dragging a
drawing from about 300 to under 10.

Adding a detector: [detectors.md](detectors.md#adding-a-detector). Adding a data
source: [data-sources.md](data-sources.md#adding-a-source).
