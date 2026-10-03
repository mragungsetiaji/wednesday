# Data sources and storage

Wednesday reads closed 1-minute bars and builds every higher timeframe from
them. Where those bars come from is a setting you can change live.

## Sources

| Source | Cost | Notes |
| --- | --- | --- |
| `yfinance` (default) | Free | COMEX gold futures `GC=F`, not spot; Yahoo only serves ~7 days of 1-minute bars |
| `mt5` | Your broker | Spot XAUUSD from a running MT5 terminal on the same Windows machine |
| `csv` | - | M1 bars from a file another process keeps appending to (`time, open, high, low, close[, volume]`) |
| `synthetic` | - | Demo data, never stored: the newest real bars in the database, then a random walk from them (see below) |

When the database holds real bars (MT5 first, otherwise Yahoo or CSV; never the
sample journal's), the demo shows them: the last 45 days of real candles as
stored, then a random walk from the last real close to now, and on live. Before
the first real bar a made-up walk leads into its open, since a scan needs more
history than a few stored days. So the recent chart is real gold and only the
part after the last stored bar is invented. With no real bars stored, the whole
demo is a random walk from 2650.

```bash
uv sync                      # default setup (Yahoo Finance + SQLite)
uv sync --extra mt5          # MetaTrader 5 feed (Windows only)
uv sync --extra postgres     # PostgreSQL storage
uv sync --extra llm          # Claude / OpenAI SDKs for the news brief
uv sync --extra ml           # scikit-learn + pyarrow for the Lab (training, model files, Parquet)
```

## Picking a source

Open the dashboard and go to **Settings > Data source** (the menu on the left picks
a section). Saving restarts the feed live, no server restart needed.

![Settings page](images/settings.png)

Where a setting comes from, strongest first:

1. A command line flag (`--source mt5`)
2. The choice saved from the dashboard
3. `XAU_SOURCE` in `.env`
4. The default (`yfinance`)

`--reset-settings` forgets the saved choice. Switching the source resets the
symbol to that source's default (`GC=F` for Yahoo, `XAUUSD` for MT5).

```bash
uv run wednesday --source mt5 --symbol XAUUSD # MT5 terminal running and logged in
uv run wednesday --source csv --csv data/xauusd_m1.csv
uv run wednesday --source synthetic --once    # demo data, one scan
```

## Feed clock

Bar times are kept in the feed's own clock (candles line up with your MT5
chart). The quarterly view converts them to New York time, so it needs to know
that clock. Set it in **Settings > Data source > Feed clock**, `XAU_CLOCK` or `--clock`:

| Value | Meaning |
| --- | --- |
| `UTC` | Default for Yahoo Finance, CSV and demo data |
| `NY+7` | New York time plus 7 hours, default for MT5: the server time of most gold brokers (midnight there is the 17:00 NY close, UTC+2 in winter and UTC+3 in summer) |
| `UTC+2`, `UTC+3`, ... | A fixed offset with no daylight saving |
| `Europe/London`, `Asia/Jakarta`, ... | Any IANA time zone |

If your broker doesn't follow the NY+7 convention, compare the time of the last
candle in MT5 with the time in New York and pick the matching value.

## Live price

The scan runs once a minute, after each M1 candle closes. Between scans the
screener polls the live price and folds it into the forming M1 candle, so the
header price, the ladder and the chart's last candle move without waiting for the
next scan. When the minute closes, the scan fetches only that closed bar (history
already loaded is never fetched again) and it replaces the candle the ticks built,
since ticks sampled every second or more can miss the minute's true high or low.
With MT5 every tick since the last poll is read (`copy_ticks_from`), so the
forming candle's high and low are exact and the interval only sets how quickly
the price moves on screen. Ticks are polled on the feed thread, which never runs
the scan, so a slow scan doesn't freeze the price.

The dashboard gets the price pushed over `/api/stream` (server-sent events) the
moment it changes, at most once per screen frame, instead of asking for it. While
the stream reconnects it falls back to polling `/api/tick`, and a hidden tab stops
both until it's shown again.

Set the interval in **Settings > Data source > Live price every**, `XAU_TICK` in
`.env`, or leave it empty for the source's default:

| Source | Default | Fastest |
| --- | --- | --- |
| MT5 | 0.25 s | 0.1 s |
| Demo data | 2 s | 0.5 s |
| Yahoo Finance | 60 s (once a minute, with the scan) | 15 s: each update is a request to Yahoo |
| CSV | none: a CSV has closed bars only | - |

`0` turns live prices off. A tick only starts a forming candle within 5 minutes of
the last closed bar, so a closed market doesn't draw a candle after the gap.

## MetaTrader 5

The MT5 Python API talks to a terminal running on the same Windows machine, so
the screener and the terminal always live together. Test the connection first:

```bash
uv run wednesday --source mt5 --check
```

If the terminal is already open and logged in, no credentials are needed.
Otherwise set the terminal, login and server in **Settings** (or `MT5_PATH`,
`MT5_LOGIN`, `MT5_SERVER` in `.env`). Settings lists the MT5 terminals installed
on the PC, for when there are several. The password is entered in Settings and
kept in Windows Credential Manager (through `keyring`), never in the database;
`MT5_PASSWORD` in the environment still works for older setups.

Connecting starts the terminal at `MT5_PATH` once. If that fails, the feed
records the error and stops instead of retrying (a retry would open the
terminal again); a lost connection is retried up to 3 times, a minute apart,
and only while the terminal is still running. **Reconnect** in Settings starts
it again. Other sources keep retrying every minute.
Full Windows and VPS setup: [deploy-windows.md](deploy-windows.md).

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

- **MT5**: the terminal's own candles of that timeframe (H1, H4, ...), not M1,
  so going back months is quick. How far depends on the history the terminal
  has downloaded and on **Max bars in chart**.
- **Yahoo Finance and CSV**: the M1 bars stored in the database, so as far back
  as the screener has been running (Yahoo itself only gives ~7 days of M1), or
  as far as the files imported in [Lab > Data](lab.md#data).
- **Demo data**: only what is in memory.

Older candles show prices only; levels are drawn from the latest scan.

## Adding a source

Subclass `feeds.DataFeed` and implement `fetch_m1(count)`, returning closed M1
bars oldest first, then register it in `settings.py` (`SOURCES`) and
`feeds.build_feed`.
