# Development

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
src/xau_screener/
  timeframes.py   timeframe list + M1 -> higher timeframe resampling
  structure.py    shared swings / break of structure / ATR (computed once per timeframe)
  levels.py       Level: the common output of every detector
  detectors/      order blocks, liquidity (BSL/SSL, EQH/EQL), inducement + registry
  feeds.py        MT5, yfinance, CSV, synthetic feeds + rolling M1 buffer
  scanner.py      multi-timeframe scan, nearest level above/below, limit setups
  report.py       console table
  engine.py       scan loop shared by the console and the web server
  server.py       FastAPI: /api/scan, /api/candles, /api/settings, /api/alerts, serves the dashboard
  settings.py     data source settings, source catalog, precedence rules
  alerts.py       Telegram alerts when price enters an order block
  storage.py      SQLAlchemy store: settings, M1 history, alert log (SQLite / PostgreSQL)
  cli.py          command line entry point
web/              React + Vite + TypeScript dashboard (lightweight-charts)
scripts/          Windows VPS: auto-restart wrapper + Task Scheduler installer
docs/             these docs
tests/
```

## How a scan flows

1. `M1Buffer` loads stored bars, fetches the newest ones from the feed and saves them.
2. `resample_ohlcv` builds each timeframe, dropping the candle still forming.
3. Per timeframe, a shared `Context` computes structure once; each detector returns `Level`s.
4. `scanner` picks the nearest level above/below per detector and ranks the limit setups.
5. `Runtime` runs the Telegram alert check, then the console report or the API picks up the result.

Adding a detector: [detectors.md](detectors.md#adding-a-detector). Adding a data
source: [data-sources.md](data-sources.md#adding-a-source).
