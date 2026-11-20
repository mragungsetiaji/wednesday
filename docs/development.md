# Development

Run from source (macOS, Linux or Windows):

```bash
git clone https://github.com/mragungsetiaji/wednesday.git
cd wednesday
make setup       # checks uv + Node, creates .env, installs everything, builds the dashboard
make serve       # dashboard on http://127.0.0.1:8000 (Yahoo Finance data)
make demo        # the same on random demo data
make tauri       # the desktop app from this checkout (needs Rust)
make             # list every target
```

Extra flags go through `ARGS`, e.g. `make serve PORT=9000 ARGS="--lookback 300"`.
On Windows without make, run the `uv` commands directly: see
[Windows and VPS](deploy-windows.md).

```bash
make install     # Python deps (uv) + dashboard deps (npm)
make test        # pytest
make typecheck   # TypeScript
make dev         # demo API + Vite hot reload on http://localhost:5173
```

Needs [uv](https://docs.astral.sh/uv/) and Node 20+. Without make:

```bash
uv sync && uv run pytest
cd web && npm install && npm run dev
```

## Project layout

```
src/wednesday/
  timeframes.py   timeframe list + M1 -> higher timeframe resampling
  structure.py    shared swings / break of structure / ATR (computed once per timeframe)
  levels.py       Level: the common output of every detector
  detectors/      order blocks, liquidity (BSL/SSL, EQH/EQL), inducement + registry
  feeds.py        yfinance, TradingView, CSV, synthetic feeds + rolling M1 buffer
  scanner.py      multi-timeframe scan, nearest level above/below, limit setups
  quarters.py     quarterly theory blocks (weekday, session, 90m) in New York time + green stats
  report.py       console table
  engine.py       scan loop shared by the console and the web server
  server.py       FastAPI: /api/scan, /api/candles, /api/tick and /api/stream (live price, pushed), /api/quarters, /api/bias, /api/brief, /api/calendar (+ /reactions, /import), /api/settings, /api/alerts, serves the dashboard
  settings.py     data source settings, source catalog, precedence rules
  alerts.py       Telegram alerts when price enters an order block
  bias.py         the trader's bias, its expiry, and the risk on / off label per setup
  news.py         economic calendar (ForexFactory weekly feed) for the risk-time card; every week kept in calendar_events
  news_stats.py   gold's move after past releases per event type (M1, feed clock), calendar CSV import
  brief.py        LLM news brief (Claude or OpenAI): fetch news pages, ask, suggest a bias
  storage.py      SQLAlchemy store: settings, M1 history, alert log, journals (SQLite / PostgreSQL)
  history.py      older candles for the chart as it scrolls left (memory, then stored M1)
  history_import.py  M1 file import (MT5, Dukascopy, HistData, CSV) into the stored bars, /api/history
  journal/        MT5 accounts: HTML report import, gain and drawdown rebuilt from M1 bars, the sample portfolio, /api/journals
  plugins.py      loads plugins (entry point group wednesday.plugins): routes, hooks, jobs
  cli.py          command line entry point
web/              React + Vite + TypeScript dashboard (lightweight-charts)
src-tauri/        the Tauri desktop shell (#92)
server-rs/        the Rust server in front of the Python one (#96)
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

The backend moves to Rust a route at a time. `server-rs/` is a small
[axum](https://github.com/tokio-rs/axum) server that sits on the dashboard's address,
answers the routes listed in `PORTED` (`server-rs/src/lib.rs`) and passes every other
request to the Python server, streaming the answer back. The Tauri shell runs it in
front of the sidecar, so the app works the same at every step.

To port a route:

1. Add its handler to `app()` in `server-rs/src/lib.rs` and the route to `PORTED`.
2. Its answer must have the shape `tests/api_contract.json` gives it; the Rust tests
   check every ported GET route against that file. A route that scans must also
   match `tests/golden/`.
3. Routes behind the login stay in Python until the login itself is ported: the Rust
   tests refuse a ported route that isn't in `auth.PUBLIC`.

```bash
make test-tauri                 # the shell's and the Rust server's tests
make serve PORT=8001            # the Python server ...
make serve-rs PORT=8000         # ... and the Rust one in front, on http://127.0.0.1:8000
```

The Rust server only listens on `127.0.0.1` for now: the login, and the address it
counts failed logins by, are still Python's.

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
per second. With 4 panes on the demo feed, Chrome's task time over 75 s went from
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
