# xau-screener

Smart Money Concepts screener for XAUUSD. Every minute it pulls closed
1-minute bars, resamples them into **4H, 1H, 30M, 15M and 5M** candles, runs
several detectors on the last N candles of each timeframe (**order blocks**,
**BSL/SSL liquidity** with equal highs/lows, **inducement**), and reports the
**nearest active level above and below** price for each detector, scanning from
the highest timeframe down to the lowest.

```
[2026-09-26 02:47] XAUUSD price 2473.79
TF   DET    #  NEAREST ABOVE                      NEAREST BELOW                      NOTES
4H   OB     5  BEAR OB 2548.98-2561.48 (+75.19) t0 -                                  -
     LIQ    7  BSL 2588.64 (+114.84)              SSL 2429.90 (-43.89)               -
     IDM    0  -                                  -                                  -
1H   OB     5  BEAR OB 2504.24-2514.89 (+30.44) t0 BULL OB 2434.85-2440.92 (-32.87) t0 -
     LIQ    9  BSL 2535.36 (+61.56)               SSL 2434.85 (-38.95)               -
     IDM    1  IDM ▼ 2485.15 (+11.35)             -                                  -
...
nearest OB: above 5M BEAR OB 2475.37-2478.77 (+1.58) t2 | below 5M BULL OB 2468.32-2471.79 (-2.01) t0
nearest LIQ: above 5M BSL 2478.77 (+4.97) | below 5M SSL 2468.81 (-4.99)
nearest IDM: above 1H IDM ▼ 2485.15 (+11.35) | below 15M IDM ▲ 2439.56 (-34.23)
```

`(+4.50)` is the distance from price to the level's nearest edge, `tN` is how
many candles have tapped an order block since it formed, and *NOTES* lists levels
price is inside plus recent sweeps/mitigations. See [Detectors](#detectors).

## Quick start (macOS / Linux)

```bash
make setup     # checks uv + Node, creates .env, installs everything, builds the dashboard
make serve     # dashboard on http://127.0.0.1:8000 (Yahoo Finance data by default)
make demo      # same on random demo data, if you just want to look around
make           # list every target: dev (hot reload), scan, check, test, telegram-chats, ...
```

Extra CLI flags go through `ARGS`, e.g. `make serve PORT=9000 ARGS="--lookback 300"`.
On Windows, use the `uv` commands below directly (see [docs/DEPLOY_WINDOWS.md](docs/DEPLOY_WINDOWS.md)).

## Setup (uv)

```bash
uv sync                      # everything for the default setup (Yahoo Finance + SQLite)
uv sync --extra mt5          # MetaTrader 5 feed (Windows only)
uv sync --extra postgres     # PostgreSQL storage
```

## Run

```bash
uv run xau-screener --serve                      # dashboard on http://127.0.0.1:8000, Yahoo Finance data

# Console only, or pick a source explicitly
uv run xau-screener --source mt5 --symbol XAUUSD # MT5 terminal running and logged in
uv run xau-screener --source csv --csv data/xauusd_m1.csv
uv run xau-screener --source synthetic --once    # random-walk demo, no data needed
```

## Data sources and storage

| Source | Cost | Notes |
| --- | --- | --- |
| `yfinance` (default) | Free | COMEX gold futures `GC=F`, not spot; Yahoo only serves ~7 days of 1-minute bars |
| `mt5` | Your broker | Spot XAUUSD from a running MT5 terminal on the same Windows machine |
| `csv` | - | M1 bars from a file another process keeps appending to |
| `synthetic` | - | Random-walk demo data, never stored |

Pick the source in the dashboard under **Settings**: saving restarts the feed
live, no server restart. The choice is saved and wins over `XAU_SOURCE` in
`.env`; an explicit `--source` flag wins over both (`--reset-settings` forgets
the saved choice). The MT5 password is only read from `MT5_PASSWORD` in `.env`
and never stored.

Every fetched M1 bar is stored per source and symbol, so a restart resumes
from the database instead of refetching, and Yahoo's 7-day window stops being a
limit: history keeps growing while the screener runs (4H with the default
lookback wants ~48k M1 bars, about 5 weeks). Storage is **SQLite** at
`data/xau.db` by default. For **PostgreSQL**, set
`XAU_DB_URL=postgresql+psycopg://user:pass@host:5432/xau` and run
`uv sync --extra postgres`; the tables are created on first start.
`--db none` turns storage off.

## Web dashboard (React)

![Dashboard (synthetic demo data)](docs/dashboard.png)

```bash
cd web && npm install && npm run build && cd ..   # once, and after UI changes (needs Node 20+)
uv run xau-screener --serve                        # scan loop + dashboard on http://127.0.0.1:8000
```

The chart comes first. Beside it, a **price ladder** lists what matters around
price in the same vertical order as the chart's price axis: sell limit setups
(`S1`–`S3`, extreme first) and the nearest liquidity/IDM above, the live price,
then buy setups (`B1`–`B3`) and levels below. Every ladder row is pinned on the
chart under the same tag: setups as an entry line with a dashed stop, levels as
lines. Hovering a row highlights it on the chart and dims the rest; a level
outside the visible range docks to the chart edge with its price. Clicking a row
opens its timeframe.

The chart also shows the charted timeframe's order blocks (boxes, mid OBs
lighter), liquidity and IDM, higher-timeframe levels faded, the latest break of
structure (BOS/CHoCH) as a dashed segment, and recent sweeps as markers. The
timeframe tabs carry each timeframe's structure direction. Structure, events and
an all-timeframes table sit below the chart. It refreshes every few seconds; the
server rescans once a minute. API docs: `http://127.0.0.1:8000/api/docs`.

For UI development, run the server and the Vite dev server side by side:

```bash
uv run xau-screener --source synthetic --serve
cd web && npm run dev        # http://localhost:5173, proxies /api to :8000
```

The dashboard has no login. It binds to `127.0.0.1` by default; see
[docs/DEPLOY_WINDOWS.md](docs/DEPLOY_WINDOWS.md) for reaching it on a VPS.

## MT5 and options

For MT5, test the connection first with `uv run xau-screener --check`.
Settings can live in a `.env` file (copy `.env.example`); command line flags
override it. Windows local testing and VPS production setup (auto-start,
auto-restart, logs) are in [docs/DEPLOY_WINDOWS.md](docs/DEPLOY_WINDOWS.md).

Useful options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--timeframes` | `4H,1H,30M,15M,5M` | Timeframes to scan (always processed high to low) |
| `--lookback` | `200` | Closed candles per timeframe searched for order blocks |
| `--swing-length` | `5` | Bars on each side needed to confirm a swing high/low |
| `--zone` | `body` | OB level covers the body (`body`) or the full candle range (`wick`) |
| `--mitigation` | `wick` | OB is taken by a wick through the whole body (`wick`) or a close beyond it (`close`) |
| `--max-sl` | `3.0` | Max stop distance for OB limit setups, in price units (`XAU_MAX_SL`) |
| `--detectors` | `ob,liquidity,idm` | Detectors to run (`XAU_DETECTORS`) |
| `--eq-tolerance` | `0.1` | Equal highs/lows: max gap as a multiple of ATR(14) |
| `--idm-length` | `2` | Internal swing bars each side for inducement |
| `--recent-bars` | `3` | Report levels swept/mitigated within this many candles |
| `--once` | off | Scan once and exit instead of looping every minute |
| `--serve` | off | Also run the web dashboard + API (`XAU_SERVE=1`) |
| `--host` / `--port` | `127.0.0.1` / `8000` | Dashboard address (`XAU_HOST`, `XAU_PORT`) |
| `--db` | `sqlite:///data/xau.db` | Storage URL (`XAU_DB_URL`); `none` disables it |
| `--check` | off | Test the feed connection (account, symbol, bars loaded) and exit |
| `--log-file` | - | Rotating log file, includes every scan table (`XAU_LOG_FILE`) |
| `--json-out` | - | Append each scan as a JSON line (for bots/dashboards) |
| `--delay` | `2` | Seconds after the minute closes before polling |

The 4H timeframe with `--lookback 200` needs about 48k M1 bars, which are loaded
once at startup; after that only the last 30 bars are fetched each minute.

## Telegram alerts

When price trades into an active order block, the screener sends a Telegram
message with the timeframe, extreme/mid, the limit entry, stop and risk, and
that timeframe's structure. The check uses the high/low of the new M1 bars, so
a wick into the zone counts even if the minute closes outside it. Each order
block alerts once (the log is kept in the database, so restarts don't repeat
alerts), and bars from before the order block was confirmed never trigger it.

1. Create a bot with `@BotFather` and put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send the bot any message, run `make telegram-chats` (or `uv run xau-screener --telegram-chats`)
   and put the printed id in `.env` as `TELEGRAM_CHAT_ID`.
3. Restart, then `make telegram-test` or **Settings > Send test message**.

In **Settings** you choose which timeframes and which order blocks (extreme,
mid) alert, pause alerts, and see the recent alert log. The token and chat id
stay in `.env`; they are never written to the database.

## Detectors

All detectors run on the same closed candles of each timeframe and share one
market-structure pass, then report the same kind of object (a `Level`: a zone,
or a line when top == bottom). Choose them with `--detectors ob,liquidity,idm`
(default: all).

**Shared structure** (`structure.py`)

- **Swings**: a swing high/low is the extreme of `--swing-length` bars on each
  side. It only exists once those right-side bars have closed (no look-ahead).
- **Break of structure (BOS)**: a candle *closes* above the latest unbroken
  swing high (bullish) or below the latest unbroken swing low (bearish).

**Order blocks** (`ob`), built for limit entries:

- The OB candle is the **opposite-colour candle**: red for a buy OB, green for a
  sell OB (the candle before the impulse, not the impulsive candle itself).
- **Extreme OB** (high priority): the last red candle at or before the lowest low
  of the leg that broke structure up (sell: last green candle at or before the
  highest high). **Mid OBs** (low priority): red candles later in the move that are
  followed by an impulsive green candle closing above their body (sell: mirror).
- **Limit plan**: buy limit at the top of the red body, stop at its bottom; sell
  limit at the bottom of the green body, stop at its top. The stop is capped at
  `--max-sl` (default 3.00) from the entry.
- **Taken**: an OB is removed once a later wick trades through its whole body
  (`--mitigation close`: a close beyond it instead). A wick that only reaches the
  entry keeps it valid and counts as *entry tested*.
- `--zone body` (default) makes the level cover the body; `--zone wick` the full
  candle range. Entry and stop always come from the body.

**Limit setups**: across all timeframes, untaken bearish OBs whose entry is above
price are sell-limit candidates and bullish OBs below price are buy-limit
candidates, ranked **extreme first, then nearest entry**. The console prints the
top 3 per side (`SELL LIMIT #1 5M extreme entry ... SL ... (risk ...)`), the API
returns them under `scan.setups`, and the dashboard shows them at the top.

**Liquidity** (`liquidity`): every swing high not yet traded through is
**BSL** (buy stops above), every such swing low is **SSL**. Active swings within
`--eq-tolerance` × ATR(14) of each other merge into an **EQH / EQL** pool. A level
ends when a wick trades through it: a *grab* if that candle closes back inside,
a *break* if it closes beyond.

**Inducement** (`idm`): after a BOS, the first internal swing (built with
`--idm-length` bars each side) against the move: the first pullback low after a
bullish BOS, the first pullback high after a bearish BOS. Only the latest IDM per
direction is kept; it ends when swept (grab/break as above).

**Events**: levels that ended within the last `--recent-bars` candles of a
timeframe are reported as events (console *NOTES* column, dashboard *Recent
events* panel), e.g. `SSL 2468.81 grabbed @ 14:05`.

### Adding a detector

1. Create `src/xau_screener/detectors/<name>.py` with a `Detector` subclass:
   set `name` and `title`, implement `detect(ctx) -> list[Level]`. Use
   `ctx.structure(length)` for swings/BOS and `ctx.atr` instead of recomputing,
   and set `ended_time` on levels that were mitigated/swept.
2. Register the class in `detectors/__init__.py` (`REGISTRY`).
3. If it needs a new setting, add it to `DetectorParams` and a CLI flag.

The scanner, console table, API and dashboard (layer toggle, tables, chart)
pick it up automatically. In the chart a new detector is drawn with the neutral
dotted style until it gets its own role in `web/src/format.ts` (`roleOf`).

Only closed candles are used for detection; the still-forming candle of each
timeframe is dropped. Candle times follow the feed's clock, so with MT5 the 4H
candles line up with the broker chart.

## Project layout

```
src/xau_screener/
  timeframes.py   timeframe list + M1 -> HTF resampling
  structure.py    shared swings / break of structure / ATR (computed once per timeframe)
  levels.py       Level: the common output of every detector
  detectors/      order blocks, liquidity (BSL/SSL, EQH/EQL), inducement + registry
  feeds.py        MT5, yfinance, CSV, synthetic feeds + rolling M1 buffer
  scanner.py      multi-timeframe scan, nearest level above/below per detector
  report.py       console table
  engine.py       scan loop shared by the console and the web server
  server.py       FastAPI: /api/scan, /api/candles, serves the built dashboard
  settings.py     data source settings, source catalog, precedence rules
  alerts.py       Telegram alerts when price enters an order block
  storage.py      SQLAlchemy store: saved settings + M1 history (SQLite / PostgreSQL)
  cli.py          command line entry point
web/              React + Vite + TypeScript dashboard (lightweight-charts)
scripts/          Windows VPS: auto-restart wrapper + Task Scheduler installer
docs/             deployment guide
tests/
```

Add a new data source by subclassing `feeds.DataFeed` and implementing
`fetch_m1(count)` (return closed M1 bars, oldest first).

## Tests

```bash
uv run pytest
```
