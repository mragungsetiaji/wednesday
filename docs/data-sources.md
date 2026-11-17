# Data sources and storage

Wednesday reads closed 1-minute bars and builds every higher timeframe from
them. Where those bars come from is a setting you can change live.

## Sources

| Source | Cost | Notes |
| --- | --- | --- |
| `yfinance` (default) | Free | COMEX gold futures `GC=F`, not spot; Yahoo only serves ~7 days of 1-minute bars |
| `tradingview` | Free | Spot gold from TradingView, `FX:XAUUSD` (FXCM) by default; ~5 days of 1-minute bars at first. Needs Node 20+ when run from source; the Windows app bundles it (see [TradingView](#tradingview)) |
| `csv` | - | M1 bars from a file another process keeps appending to (`time, open, high, low, close[, volume]`) |
| `synthetic` | - | Demo data, never stored: the newest real bars in the database, then a random walk from them (see below) |

MetaTrader 5 is no longer a data source: connecting to the terminal froze it when
the Windows app started. A source saved as `mt5` by an older version, or
`XAU_SOURCE=mt5`, falls back to Yahoo Finance with a warning in the log; pick
another one in Settings. To use your broker's own prices, export M1 bars from
MT5 and import them in [Lab > Data](lab.md#data), or keep a CSV up to date for
the `csv` source.

When the database holds real bars (the most recent stream of any real source; never the
sample journal's), the demo shows them: the last 45 days of real candles as
stored, then a random walk from the last real close to now, and on live. Before
the first real bar a made-up walk leads into its open, since a scan needs more
history than a few stored days. So the recent chart is real gold and only the
part after the last stored bar is invented. With no real bars stored, the whole
demo is a random walk from 2650.

```bash
uv sync                      # default setup (Yahoo Finance + SQLite)
uv sync --extra postgres     # PostgreSQL storage
uv sync --extra llm          # Claude / OpenAI SDKs for the news brief
uv sync --extra ml           # scikit-learn + pyarrow for the Lab (training, model files, Parquet)
```

## Picking a source

Open the dashboard and go to **Settings > Data source** (the menu on the left picks
a section). Saving restarts the feed live, no server restart needed.

![Settings page](images/settings.png)

Where a setting comes from, strongest first:

1. A command line flag (`--source tradingview`)
2. The choice saved from the dashboard
3. `XAU_SOURCE` in `.env`
4. The default (`yfinance`)

`--reset-settings` forgets the saved choice. Switching the source resets the
symbol to that source's default (`GC=F` for Yahoo, `FX:XAUUSD` for TradingView,
`XAUUSD` for CSV).

```bash
uv run wednesday --source tradingview                  # FX:XAUUSD, FXCM spot gold
uv run wednesday --source tradingview --symbol OANDA:XAUUSD
uv run wednesday --source csv --csv data/xauusd_m1.csv
uv run wednesday --source synthetic --once    # demo data, one scan
```

## Feed clock

Bar times are kept in the feed's own clock. Yahoo Finance and TradingView give
UTC; a CSV file is in whatever clock wrote it. The quarterly view converts them to New York time, so it needs to know
that clock. Set it in **Settings > Data source > Feed clock**, `XAU_CLOCK` or `--clock`:

| Value | Meaning |
| --- | --- |
| `UTC` | Default for Yahoo Finance, TradingView, CSV and demo data |
| `NY+7` | New York time plus 7 hours: the server time of most gold brokers (midnight there is the 17:00 NY close, UTC+2 in winter and UTC+3 in summer), for a CSV written in broker time |
| `UTC+2`, `UTC+3`, ... | A fixed offset with no daylight saving |
| `Europe/London`, `Asia/Jakarta`, ... | Any IANA time zone |

For a CSV in broker time whose broker doesn't follow the NY+7 convention,
compare the time of the last candle in the broker's platform with the time in
New York and pick the matching value.

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
| Demo data | 2 s | 0.5 s |
| Yahoo Finance | 60 s (once a minute, with the scan) | 15 s: each update is a request to Yahoo |
| CSV | none: a CSV has closed bars only | - |

`0` turns live prices off. A tick only starts a forming candle within 5 minutes of
the last closed bar, so a closed market doesn't draw a candle after the gap.

## TradingView

The `tradingview` source reads TradingView's market data through
[TradingView-API](https://github.com/Mathieu2301/TradingView-API)
(`@mathieuc/tradingview`), a community library. It is **unofficial**: TradingView
doesn't offer this as an API, its terms don't allow automated access to its data,
and the library can stop working when TradingView changes its site. Use it for
personal charts, and keep Yahoo Finance as a fallback.

It runs as a small Node helper (`tradingview/feed.mjs`) that the screener starts
and keeps running, with one websocket open. The Windows app bundles Node.js and
the helper, so there it works with nothing to install. Run from source, it needs
Node 20+ on the PATH and the helper's packages:

```bash
make install                      # also installs the helper (npm install in tradingview/)
npm install --prefix tradingview  # the helper alone
uv run wednesday --source tradingview --check
```

The symbol takes TradingView's exchange prefix. `FX:` is FXCM:

| Symbol | Market |
| --- | --- |
| `FX:XAUUSD` (default) | XAU/USD spot, FXCM |
| `FX:XAGUSD` | XAG/USD spot, FXCM |
| `OANDA:XAUUSD`, `PEPPERSTONE:XAUUSD`, ... | Other brokers' gold |

`FXCM:XAUUSD` doesn't exist; the source then says to use `FX:XAUUSD`. Without a
TradingView account the server gives about 7,000 one-minute bars (~5 trading
days), so the 4H timeframe fills in as stored history grows, as with Yahoo. Bar
times are UTC. The candle still forming is dropped; the live price comes from
TradingView's quote and builds the forming candle between scans.

The same helper feeds a full-screen chart set to another symbol (`FX:XAGUSD`
beside gold, see [Dashboard](dashboard.md#full-screen-and-multiple-charts)),
whatever the main source is: only its candles and live price, through
`/api/markets/candles` and `/api/markets/tick`. Nothing about that symbol is
scanned or stored.

## Storage

Every fetched M1 bar is stored per source and symbol. A restart resumes from
the database instead of refetching, and Yahoo's 7-day window stops being a
limit: history keeps growing while the screener runs. The 4H timeframe with the
default lookback wants ~48k M1 bars, about 5 weeks.

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

- **Yahoo Finance, TradingView and CSV**: the M1 bars stored in the database, so as far back
  as the screener has been running (Yahoo itself only gives ~7 days of M1), or
  as far as the files imported in [Lab > Data](lab.md#data).
- **Demo data**: only what is in memory.

Older candles show prices only; levels are drawn from the latest scan.

## Adding a source

Subclass `feeds.DataFeed` and implement `fetch_m1(count)`, returning closed M1
bars oldest first, then register it in `settings.py` (`SOURCES`) and
`feeds.build_feed`.
