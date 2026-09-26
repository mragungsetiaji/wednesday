# Data sources and storage

Wednesday reads closed 1-minute bars and builds every higher timeframe from
them. Where those bars come from is a setting you can change live.

## Sources

| Source | Cost | Notes |
| --- | --- | --- |
| `yfinance` (default) | Free | COMEX gold futures `GC=F`, not spot; Yahoo only serves ~7 days of 1-minute bars |
| `mt5` | Your broker | Spot XAUUSD from a running MT5 terminal on the same Windows machine |
| `csv` | - | M1 bars from a file another process keeps appending to (`time, open, high, low, close[, volume]`) |
| `synthetic` | - | Random-walk demo data, never stored |

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

## Adding a source

Subclass `feeds.DataFeed` and implement `fetch_m1(count)`, returning closed M1
bars oldest first, then register it in `settings.py` (`SOURCES`) and
`feeds.build_feed`.
