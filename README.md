<div align="center">

# Wednesday

<sub>part of momentum.id</sub>

<p><i>"We're counting candles, not gambling. We follow a very specific set of rules and run a system.<br>
I've seen how crazy people get at those tables. Sometimes you lose control.<br>
They follow their emotions. And you will not!"</i><br>
<sub>— Prof. Micky Rosa, MIT, <i>21</i> (2008)</sub></p>

**Smart Money Concepts screener for XAUUSD.**<br>
Order blocks, liquidity and inducement across 4H → 5M, with ranked limit setups and Telegram alerts.

<p>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white" alt="uv">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React_19-20232A?logo=react&logoColor=61DAFB" alt="React 19">
  <img src="https://img.shields.io/badge/TypeScript-3178C6?logo=typescript&logoColor=white" alt="TypeScript">
  <br>
  <img src="https://img.shields.io/badge/data-Yahoo_Finance_%7C_MetaTrader_5-8f5b00" alt="Data: Yahoo Finance or MetaTrader 5">
  <img src="https://img.shields.io/badge/storage-SQLite_%7C_PostgreSQL-003B57?logo=sqlite&logoColor=white" alt="Storage: SQLite or PostgreSQL">
  <img src="https://img.shields.io/badge/alerts-Telegram-26A5E4?logo=telegram&logoColor=white" alt="Telegram alerts">
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/detectors.md">Strategy</a> ·
  <a href="docs/dashboard.md">Dashboard</a> ·
  <a href="docs/alerts.md">Alerts</a> ·
  <a href="docs/README.md">All docs</a>
</p>

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/images/dashboard-light.png">
  <img src="docs/images/dashboard-dark.png" alt="Wednesday dashboard: chart with order blocks and liquidity, and the level ladder beside it" width="100%">
</picture>

</div>

## What it does

- **Scans every minute**: 1-minute bars become 4H, 1H, 30M, 15M and 5M candles, scanned from the highest timeframe down.
- **Finds the nearest levels** above and below price: order blocks (extreme and mid), BSL / SSL with equal highs and lows, and inducement.
- **Ranks limit setups**: entry on the order block's body, stop on the opposite edge capped at 3.00, extreme first, then nearest.
- **Maps everything to the chart**: every setup and level in the ladder is pinned on the chart under the same tag.
- **Quarterly theory pane** under the chart: every weekday, session (Tokyo, London, NY AM, NY PM) and 90-minute quarter as a green or red block, plus how often each one closed green.
- **Pings you on Telegram** when price trades into an order block.
- **Free data by default** (Yahoo Finance), MetaTrader 5 when you're ready; history stored in SQLite or PostgreSQL.

<table>
  <tr>
    <td width="50%"><img src="docs/images/ladder-highlight.png" alt="Hovering a setup in the ladder highlights it on the chart"></td>
    <td width="50%"><img src="docs/images/settings.png" alt="Settings: data source, storage and Telegram alerts"></td>
  </tr>
  <tr>
    <td align="center"><sub>Hover a setup to find it on the chart</sub></td>
    <td align="center"><sub>Switch data source live, set up alerts</sub></td>
  </tr>
</table>

> **Why "Wednesday"?** Watching gold through quarterly theory, the Wednesday and New York blocks kept coming up green. The quarterly pane shows those blocks on the chart and its stats count how often that holds.

## Quick start

Needs [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
git clone https://github.com/mragungsetiaji/wednesday.git
cd wednesday
make setup     # checks uv + Node, creates .env, installs everything, builds the dashboard
make serve     # dashboard on http://127.0.0.1:8000 (Yahoo Finance data)
```

Just want to look around? `make demo` runs the same dashboard on random demo data.

```bash
make dev       # API + Vite hot reload on http://localhost:5173
make scan      # one scan in the console
make test      # run the tests
make           # list every target
```

Extra flags go through `ARGS`, e.g. `make serve PORT=9000 ARGS="--lookback 300"`.

**Windows / MT5**: run the `uv` commands directly, see [Windows and VPS](docs/deploy-windows.md).

```powershell
uv sync --extra mt5
uv run wednesday --source mt5 --check   # test the MT5 connection
uv run wednesday --serve
```

## Docs

| | |
| --- | --- |
| [Detectors and the limit strategy](docs/detectors.md) | How order blocks, liquidity and inducement are found, and how setups are ranked |
| [Dashboard](docs/dashboard.md) | Chart, level ladder, quarterly pane, panels |
| [Data sources and storage](docs/data-sources.md) | Yahoo Finance, MT5, CSV, SQLite / PostgreSQL |
| [Telegram alerts](docs/alerts.md) | Bot setup and when alerts fire |
| [Configuration](docs/configuration.md) | Make targets, CLI options, `.env` |
| [Windows and VPS](docs/deploy-windows.md) | Running 24/7 next to an MT5 terminal |
| [Development](docs/development.md) | Project layout and how a scan flows |

> [!NOTE]
> Wednesday is an analysis tool. It doesn't place orders, and nothing it shows is financial advice.
