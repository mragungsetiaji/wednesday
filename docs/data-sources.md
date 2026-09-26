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
```

## Picking a source

Open the dashboard and go to **Settings**. Saving restarts the feed live, no
server restart needed.

![Settings page](images/settings.png)

Where a setting comes from, strongest first:

1. A command line flag (`--source mt5`)
2. The choice saved from the dashboard
3. `XAU_SOURCE` in `.env`
4. The default (`yfinance`)

`--reset-settings` forgets the saved choice. Switching the source resets the
symbol to that source's default (`GC=F` for Yahoo, `XAUUSD` for MT5).

```bash
uv run xau-screener --source mt5 --symbol XAUUSD # MT5 terminal running and logged in
uv run xau-screener --source csv --csv data/xauusd_m1.csv
uv run xau-screener --source synthetic --once    # demo data, one scan
```

## MetaTrader 5

The MT5 Python API talks to a terminal running on the same Windows machine, so
the screener and the terminal always live together. Test the connection first:

```bash
uv run xau-screener --source mt5 --check
```

If the terminal is already open and logged in, no credentials are needed.
Otherwise set `MT5_LOGIN`, `MT5_SERVER` and `MT5_PATH` (in Settings or `.env`).
The password is only read from `MT5_PASSWORD` in `.env` and is never stored.
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
