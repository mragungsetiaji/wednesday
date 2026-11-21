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
days), so the 4H timeframe fills in as stored history grows. Bar
times are UTC. The candle still forming is dropped; the live price comes from
TradingView's quote and builds the forming candle between scans.

The same connection feeds a full-screen chart set to another symbol (`FX:XAGUSD`
beside gold, see [Dashboard](dashboard.md#full-screen-and-multiple-charts)):
only its candles and live price, through
`/api/markets/candles` and `/api/markets/tick`. Nothing about that symbol is
scanned or stored.

## Storage

Every fetched M1 bar is stored per source and symbol. A restart resumes from
the database instead of refetching, and TradingView's few days stop being a
limit: history keeps growing while the screener runs. This stored history is the
only history there is; there is no import. The 4H timeframe with the default
lookback wants ~48k M1 bars, about 5 weeks. Synthetic data is never stored.

| Backend | How |
| --- | --- |
| SQLite (default) | `data/xau.db`, nothing to set up |
| PostgreSQL | `XAU_DB_URL=postgresql+psycopg://user:pass@host:5432/xau` and `uv sync --extra postgres`; tables are created on first start |
| Off | `--db none` |

The database also keeps the saved settings and the Telegram alert log.

## Scrolling back

The chart opens on the last `--lookback` candles of a timeframe, the ones the
scan uses. Drag it to the left and older candles load as you reach the edge,
300 at a time, with no effect on the scan. They come from the M1 bars in
memory first, then:

- **TradingView**: the M1 bars stored in the database, so as far back as the
  screener has been running on that symbol.
- **Synthetic data**: only what is in memory.

Older candles show prices only; levels are drawn from the latest scan.

## Adding a source

Subclass `feeds.DataFeed` and implement `fetch_m1(count)`, returning closed M1
bars oldest first, then register it in `settings.py` (`SOURCES`, or
`DEV_SOURCES` for one that shouldn't appear in Settings) and `feeds.build_feed`.
