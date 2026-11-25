# Data sources and storage

Wednesday reads closed 1-minute bars and builds every higher timeframe from
them. They come from TradingView, and every bar fetched is stored, so history
grows while the screener runs.

## Sources

| Source | Cost | Notes |
| --- | --- | --- |
| `tradingview` (default) | Free | Spot gold from TradingView, `FX:XAUUSD` (FXCM) by default; ~5 days of 1-minute bars at first. Nothing to install (see [TradingView](#tradingview)) |

TradingView is the only source the dashboard offers. MetaTrader 5, Yahoo Finance
and the CSV file source were removed. A source saved by an older version (`mt5`,
`yfinance` or `csv`), or set in `XAU_SOURCE`, falls back to TradingView with a
warning in the log.

For development and tests there is also `synthetic`: a random walk from 2650,
never stored and not real market data. It isn't shown in Settings; start it with
`--source synthetic` or `XAU_SOURCE=synthetic` (`make dev` does).

```bash
uv sync                      # default setup (SQLite)
uv sync --extra postgres     # PostgreSQL storage
```

## Picking a source

Open the dashboard and go to **Settings > Data source** (the menu on the left picks
a section) to set the symbol, the feed clock and the live price interval. Saving
restarts the feed live, no server restart needed.

![Settings page](images/settings.png)

Where a setting comes from, strongest first:

1. A command line flag (`--source tradingview`, `--symbol OANDA:XAUUSD`)
2. The choice saved from the dashboard
3. `XAU_SOURCE` in `.env` (with `XAU_SYMBOL`, `XAU_CLOCK`, which only count when `XAU_SOURCE` is set)
4. The default (`tradingview`, `FX:XAUUSD`)

`--reset-settings` forgets the saved choice.

```bash
uv run wednesday --source tradingview                  # FX:XAUUSD, FXCM spot gold
uv run wednesday --source tradingview --symbol OANDA:XAUUSD
uv run wednesday --source synthetic --once             # development: random walk, one scan
```

## Markets

**Settings > Markets** holds the chart's symbol menu (the `markets` table): which
TradingView symbols it lists, in what order, and the label, name and venue shown for
each. The same instrument can be listed from several venues, such as `FX:XAUUSD`
(FXCM) and `OANDA:XAUUSD`; each keeps its own candles and drawings. **Add a market**
takes TradingView's name with its exchange prefix and checks it with TradingView
first; the name and venue come from TradingView and can be changed. Deleting one
takes it off the menu and keeps its stored candles. The scanned market can't be
deleted, nor the last one left.

A market's candles load when you pick it. **Keep live** stores a market's 1-minute bars
and its candles per timeframe even while another market is scanned
(`wed-engine/src/keeper.rs`, `keeper.py`), once a minute, so its history has no gap
when you come back to it. Nothing about a kept market is scanned. TradingView keeps
only a few days of 1-minute bars, so a market left without **Keep live** for longer
comes back with a gap in its 1-minute history; its higher timeframes fill in again.

Routes: `GET /api/markets`; `POST /api/markets` `{symbol, label?}` adds one;
`PUT /api/markets` `{markets: [...]}` saves the order, labels and **Keep live** of the
whole list; `DELETE /api/markets?symbol=` removes one. Each answers with the list.

## Feed clock

Bar times are kept in the feed's own clock. TradingView gives UTC, which is the
default. The quarterly view converts bar times to New York time, so it needs to
know that clock. Set it in **Settings > Data source > Feed clock**, `XAU_CLOCK`
or `--clock`:

| Value | Meaning |
| --- | --- |
| `UTC` | Default, for TradingView and synthetic data |
| `NY+7` | New York time plus 7 hours: the server time of most gold brokers (midnight there is the 17:00 NY close, UTC+2 in winter and UTC+3 in summer) |
| `UTC+2`, `UTC+3`, ... | A fixed offset with no daylight saving |
| `Europe/London`, `Asia/Jakarta`, ... | Any IANA time zone |

With TradingView, leave it at UTC.

## Live price

The scan runs once a minute, after each M1 candle closes. Between scans the
screener polls the live price and folds it into the forming M1 candle, so the
header price, the ladder and the chart's last candle move without waiting for the
next scan. When the minute closes, the scan fetches only that closed bar (history
already loaded is never fetched again) and it replaces the candle the ticks built,
since ticks sampled every second or more can miss the minute's true high or low.
Ticks are polled on the feed thread, which never runs
the scan, so a slow scan doesn't freeze the price.

The dashboard gets the price pushed over `/api/stream` (server-sent events) the
moment it changes, at most once per screen frame, instead of asking for it. While
the stream reconnects it falls back to polling `/api/tick`, and a hidden tab stops
both until it's shown again.

Set the interval in **Settings > Data source > Live price every**, `XAU_TICK` in
`.env`, or leave it empty for the source's default:

| Source | Default | Fastest |
| --- | --- | --- |
| TradingView | 1 s | 0.5 s: read from a quote stream kept open, not a request each time |
| Synthetic (development) | 2 s | 0.5 s |

`0` turns live prices off. A tick only starts a forming candle within 5 minutes of
the last closed bar, so a closed market doesn't draw a candle after the gap.

## TradingView

The `tradingview` source reads TradingView's market data over the websocket its web
chart uses, as the community library
[TradingView-API](https://github.com/Mathieu2301/TradingView-API)
(`@mathieuc/tradingview`) documents it. It is **unofficial**: TradingView
doesn't offer this as an API, its terms don't allow automated access to its data,
and it can stop working when TradingView changes its site. Use it for
personal charts only.

The server speaks the protocol itself (`wed-engine/src/tradingview.rs`; the Python
reference has the same client in `src/wednesday/tradingview.py`) and keeps one
websocket open, reopened when TradingView drops it. Nothing else is installed or
started: no Node.js, no helper process.

```bash
uv run wednesday --source tradingview --check
```

When it stops working, the protocol is the place to look: each websocket message holds
frames `~m~<length>~m~<payload>` (heartbeats `~h~<n>` are echoed), a chart session
(`chart_create_session`, `resolve_symbol`, `create_series`, `request_more_data`) loads
bars and a quote session (`quote_create_session`, `quote_add_symbols`) streams the price.

The symbol takes TradingView's exchange prefix. `FX:` is FXCM:

| Symbol | Market |
| --- | --- |
| `FX:XAUUSD` (default) | XAU/USD spot, FXCM |
| `FX:XAGUSD` | XAG/USD spot, FXCM |
| `OANDA:XAUUSD`, `PEPPERSTONE:XAUUSD`, ... | Other brokers' gold |

`FXCM:XAUUSD` doesn't exist; the source then says to use `FX:XAUUSD`. Without a
TradingView account the server gives about 7,000 one-minute bars (~5 trading
days), so each timeframe is fetched as TradingView builds it (5M, 15M, 30M, 1H,
4H and 1D, see [Storage](#storage)): the 4H candles open on TradingView's
sessions (03:00, 07:00, ... UTC for FXCM gold) and reach years back from the
first start. Bar times are UTC. The candle still forming is dropped; the live price comes from
TradingView's quote and builds the forming candle between scans.

The same connection feeds a full-screen chart set to another symbol (`FX:XAGUSD`
beside gold, see [Dashboard](dashboard.md#full-screen-and-multiple-charts)):
only its candles and live price, through
`/api/markets/candles` and `/api/markets/tick`. Nothing about that symbol is
scanned or stored.

## Storage

Every fetched bar is stored per source, symbol and timeframe (the `bars` table).
A restart resumes from the database instead of refetching. There is no import.

With TradingView, each timeframe has its own series: the 1-minute bars of the
scan, and the 5M, 15M, 30M, 1H, 4H and 1D candles as TradingView builds them
(`wed-engine/src/ingest.rs`, `ingest.py`). A timeframe is fetched again once a
new candle of it has closed, and only closed candles are kept; the scan reads a
timeframe's own candles, and the chart adds the forming one from the live
1-minute bars. Once a minute the ingest also loads one page (5,000 candles) of
older history, highest timeframe first, until TradingView has nothing older.
The `series` table keeps each series' first and last candle, its backfill state
(`pending`, `running`, `done`) and its last error. A timeframe TradingView won't
send is noted there and the scan resamples 1-minute bars for it meanwhile.

**Settings > Data source** lists these series under Storage, grouped by source and
ticker: each timeframe's stored candles, its latest one, whether it is ingesting
live, how far its history backfill has gone (waiting, loading, done) and its last
error. It refreshes every 30 seconds, from `GET /api/series`.

Synthetic data is never stored and has no timeframes of its own: the scan
resamples its 1-minute bars, as `tests/golden/` expects.

| Backend | How |
| --- | --- |
| SQLite (default) | `data/xau.db`, nothing to set up |
| PostgreSQL | `XAU_DB_URL=postgresql+psycopg://user:pass@host:5432/xau` and `uv sync --extra postgres`; tables are created on first start |
| Off | `--db none` |

The database also keeps the saved settings and the Telegram alert log.

## Scrolling back

The chart opens on the last `--lookback` candles of a timeframe, the ones the
scan uses. Drag it to the left and older candles load as you reach the edge,
300 at a time, with no effect on the scan. They come from memory first, then:

- **TradingView**: the timeframe's own candles stored in the database, as far
  back as the backfill has reached.
- **Synthetic data**: only what is in memory, resampled from 1-minute bars.

Older candles show prices only; levels are drawn from the latest scan.

## Adding a source

Subclass `feeds.DataFeed` and implement `fetch_m1(count)`, returning closed M1
bars oldest first, then register it in `settings.py` (`SOURCES`, or
`DEV_SOURCES` for one that shouldn't appear in Settings) and `feeds.build_feed`.
