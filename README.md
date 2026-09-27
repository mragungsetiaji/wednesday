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

> [!WARNING]
> **Not financial advice. Every risk is yours.** Wednesday is an analysis tool: it never places
> orders, and its levels, setups, alerts and briefs are software output, not advice. Trading
> leveraged products can lose more than you deposit, and every decision and loss is your own
> responsibility. By using Wednesday you accept the [disclaimer and risk agreement](DISCLAIMER.md);
> the app asks you to accept it on first start.

## What it does

- **Scans every minute**: 1-minute bars become 4H, 1H, 30M, 15M and 5M candles, scanned from the highest timeframe down.
- **Finds the nearest levels** above and below price: order blocks (extreme and mid), BSL / SSL with equal highs and lows, and inducement.
- **Ranks limit setups**: entry on the order block's body, stop on the opposite edge capped at 3.00, extreme first, then nearest.
- **Maps everything to the chart**: every setup and level in the ladder is pinned on the chart under the same tag.
- **Full screen with 1, 2 or 4 charts**, TradingView style: each on its own timeframe, crosshairs linked, panes resizable by dragging.
- **Quarterly theory pane** under the chart: every weekday, session (Tokyo, London, NY AM, NY PM) and 90-minute quarter as a green or red block, plus how often each one closed green.
- **Your bias steers the list**: set bullish, bearish or neutral by hand; setups get RISK ON / RISK OFF labels, sells at a lower high (or buys at a higher low) come first, and neutral marks everything no trade.
- **News brief**: Claude or OpenAI reads the news pages you pick and suggests a bias you can apply with one click.
- **Risk-time warning**: a card in the corner an hour before high-impact US news, glowing in the last 30 minutes.
- **Lab for machine learning**: tag candles by hand (order block, liquidity, inducement), train a model on your tags, export or import model files, and mark each block the model finds on the chart valid or invalid.
- **Trading journal**: import an MT5 account (synced from the terminal or from its report) and see gain, drawdown, deposits and the growth curve, with the drawdown rebuilt from M1 prices so a statement can't hide it.
- **Pings you on Telegram** when price trades into an order block.
- **Free data by default** (Yahoo Finance), MetaTrader 5 when you're ready; history stored in SQLite or PostgreSQL.

<table>
  <tr>
    <td width="50%"><img src="docs/images/ladder-highlight.png" alt="Hovering a setup in the ladder highlights it on the chart"></td>
    <td width="50%"><img src="docs/images/settings.png" alt="Settings: a menu of sections on the left, the data source with what is running and the storage"></td>
  </tr>
  <tr>
    <td align="center"><sub>Hover a setup to find it on the chart</sub></td>
    <td align="center"><sub>Switch data source live, set up alerts</sub></td>
  </tr>
</table>

### News risk time

High-impact US releases are dashed lines on the chart (amber ahead, grey once out), and an hour before one a card appears in the corner with a countdown. In the last 30 minutes a light runs around it. Close it and it comes back once at the 30-minute mark.

<table>
  <tr>
    <td width="58%"><img src="docs/images/news-chart.png" alt="News lines and labels on the 1H chart: NFP, ISM, FOMC, Waller and CPI"></td>
    <td width="42%" valign="top"><img src="docs/images/news-card.png" alt="Risk-time card 18 minutes before CPI, with a light running around its edge"></td>
  </tr>
</table>

### Multiple timeframes at once

Full screen, split into two or four charts. Hover one and the others follow to the same moment on their own timeframe; drag the lines between charts to resize.

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/images/fullscreen-2.png">
  <img src="docs/images/fullscreen-4.png" alt="Four charts full screen (1H, 15M, 4H, 5M) with linked crosshairs" width="100%">
</picture>

<img src="docs/images/fullscreen-4-light.png" alt="Four charts full screen (30M, 15M, 4H, 5M) on gold, light theme" width="100%">

### Lab: label, train, review

Click candles and tag them, mark the stretches you've fully reviewed, and train a model on your tags: one classifier per tag, plus an outcome model that learns whether an order block traded with the limit plan reaches 2R first. Models are zip files you can pass on. On the screener, the **ML** layer shows the active model's blocks with valid / invalid buttons, and every review becomes a label for the next run. More in [docs/lab.md](docs/lab.md).

<img src="docs/images/lab-label.png" alt="The Lab: labelling 5M candles with the detector's suggestions and the model's blocks" width="100%">

### Journal

Sync an MT5 account, or import the terminal's history report, and get the numbers of a public track record: gain (time-weighted), absolute gain, drawdown, balance, equity, deposits, withdrawals and the growth curve. The drawdown is rebuilt from M1 prices minute by minute, and every deal price is checked against its bar, so a trade that sat deep in loss before closing green still counts. More in [docs/journal.md](docs/journal.md).

<img src="docs/images/journal.png" alt="The journal: account numbers on the left, the growth curve on the right" width="100%">

> **Why "Wednesday"?** Watching gold through quarterly theory, the Wednesday and New York blocks kept coming up green. The quarterly pane shows those blocks on the chart and its stats count how often that holds.

## Quick start

1. Download `WednesdaySetup-<version>.exe` from the
   [latest release](https://github.com/mragungsetiaji/wednesday/releases/latest) and run it.
   No Python or Node needed. Windows may say "Windows protected your PC" because the
   installer isn't code signed: click **More info > Run anyway**.
2. Start **Wednesday** from the Start menu or the desktop.
3. It starts on free Yahoo Finance data. For your broker's prices, open
   **Settings > Data source**, pick **MetaTrader 5**, choose your terminal and save.

More in [Windows desktop app](docs/desktop.md). Running from source, on a VPS or
developing: [Windows and VPS](docs/deploy-windows.md) and [Development](docs/development.md).

## Docs

| | |
| --- | --- |
| [Detectors and the limit strategy](docs/detectors.md) | How order blocks, liquidity and inducement are found, and how setups are ranked |
| [Dashboard](docs/dashboard.md) | Chart, level ladder, quarterly pane, panels |
| [Data sources and storage](docs/data-sources.md) | Yahoo Finance, MT5, CSV, SQLite / PostgreSQL |
| [Bias and news brief](docs/bias.md) | Setting a bias, risk on / off labels, the LLM news brief |
| [Position size](docs/risk.md) | Lot size per setup from your risk, MT5 balance and lot rules |
| [Lab: labels and models](docs/lab.md) | Tag candles, train models, model files, reviewing model blocks, the dataset |
| [Journal](docs/journal.md) | MT5 sync and report import, gain and drawdown, how the numbers are checked |
| [Telegram alerts](docs/alerts.md) | Bot setup and when alerts fire |
| [Configuration](docs/configuration.md) | Make targets, CLI options, `.env` |
| [Windows and VPS](docs/deploy-windows.md) | Running 24/7 next to an MT5 terminal |
| [Plugins](docs/plugins.md) | Add features from a separate Python package |
| [Development](docs/development.md) | Project layout and how a scan flows |

Everything above is free. Paid extras (more than one journal, signed models, the second brain, session recaps, scheduled briefs) come as a
plugin with a licence; see [Plugins](docs/plugins.md#licences-and-paid-features).

## License

Free for personal use, including trading your own accounts. Not for sale: you may not sell
Wednesday or a modified copy, charge for access to it, or offer it or its signals as a paid
service. Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE) with an added
permission for personal trading; see [LICENSE](LICENSE) for the exact terms and
[DISCLAIMER.md](DISCLAIMER.md) for the risk agreement.
