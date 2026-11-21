# AGENTS.md

Wednesday is a Smart Money Concepts screener for XAUUSD (gold): a Rust server with the
scan engine (`wed-server/`, axum), a Python reference implementation of it (`src/wednesday/`,
FastAPI, SQLAlchemy) and a React + Vite + TypeScript dashboard in `web/`. A single
discretionary trader runs it, usually as the Windows desktop app, and acts on
its levels by hand. It suggests; it never places trades.

## Commands

```bash
make install     # uv sync, plus npm install in web/
make test        # uv run pytest -q
make typecheck   # tsc -b in web/
make test-ui     # node --test on web/src/*.test.ts (Node 22.18+)
make dev         # Rust server on synthetic data + Vite hot reload on http://localhost:5173 (no market data needed)
make serve       # the Rust server and the built dashboard on http://127.0.0.1:8000
make serve-py    # the Python server from source (wednesday --serve), the reference
make scan ARGS="--source synthetic"   # one scan in the console (Python)
make tauri       # the Tauri desktop app (src-tauri), the Rust server in its process
make test-tauri  # the Rust tests (cargo test in src-tauri and wed-server)
make release v0.3.2   # tag main and push: CI builds and publishes the Windows installer
```

Python is managed by uv (3.11 locally, `>=3.10` supported); use `uv run`, not a bare
`python`. Run `make test` after Python changes, and `make typecheck` and `make test-ui`
after changes in `web/`. Tests and local runs should use `--source synthetic` (random walk,
development and tests only, not offered in the dashboard): the real source, TradingView,
goes through the Node helper in `tradingview/` and the network. TradingView is the only
market data source; Yahoo Finance, CSV files, M1 file imports and MetaTrader 5 were removed
(MT5 opened the terminal on start and froze): don't bring them back. The journal reads MT5
only through an uploaded HTML report.

## Where things are

`docs/development.md` has the module map and how a scan flows; read it before a
change that crosses modules. Feature docs live in `docs/` (one file per area), and
`docs/detectors.md` / `docs/data-sources.md` explain how to add a detector or a data
source. When a change alters behaviour a user sees, update the matching doc and, for
the dashboard, its screenshot reference if the layout changed.

`src/xau_screener/` holds only stale `__pycache__` from the project's old name; the
package is `src/wednesday/`.

The desktop app is a Tauri v2 shell (`src-tauri/`, #92). It runs the Rust server in its
own process (no Python in the installer) and exposes the commands `web/src/desktop.ts`
calls. Its commands must be listed in `src-tauri/build.rs` and granted in
`src-tauri/capabilities/default.json`, since the page comes from `http://127.0.0.1:<port>`.
The installer bundles the dashboard build, the TradingView helper and node as resources
(`tauri.release.conf.json`); `Wednesday.exe --forget-secrets` is the installer's fresh start.
Run `make test-tauri` after changes there.

The server is Rust (`wed-server/`, #96): the scan engine (`wed-engine/src/`, the
TradingView feed), the alerts, every dashboard route and the dashboard build (`ui.rs`).
Python stays in the repository as the reference: it writes `tests/golden/`, defines
`tests/api_contract.json`, and runs from source as `wednesday --serve` (`make serve-py`).
`wednesday-rs --backend <port>` (`make serve-rs`) still puts Rust in front of a Python server
to compare them, with Python following the Rust engine (`FollowFeed`, `WEDNESDAY_FOLLOW`). A
route must answer as `tests/api_contract.json` says and match `tests/golden/` where it scans,
and refuses cross-site writes as `same_site.py` does. The scan itself (resampling, structure,
detectors, setups, sessions, quarters) exists twice, `src/wednesday/` and
`wed-scan/src/`: a change to one must go into the other, and both `make test` and
`make test-tauri` check them against the same `tests/golden/`.

## Rules that matter here

- **Only closed candles.** Resampling drops the candle still forming. A detector that
  reads the forming bar, or any future bar, gives signals that look good in history
  and fail live.
- **The bias is the trader's call.** `bias.py` is set by hand. Don't make
  anything set the bias, or trade, on its own.
- **Secrets never go into the database.** The Telegram bot token is
  entered in Settings and kept in the OS credential store
  (`secret_store.py`, through `keyring`), with `.env` as a fallback. It may not be
  written to the database, logs, API responses or the dashboard: APIs take it
  write-only and report only where it comes from. `.env` is gitignored; add new
  variables to `.env.example` with an empty value and a comment.
- **Not financial advice.** Wednesday suggests; the trader decides and carries every
  risk (`DISCLAIMER.md`, accepted in the app, see `terms.py`). Don't word anything in
  the dashboard, alerts or docs as advice or a promise of results. When the
  disclaimer changes in substance, bump `TERMS_VERSION` so it is accepted again.
- **License.** PolyForm Noncommercial 1.0.0 plus personal trading use (`LICENSE`):
  free for personal use, never sold. Don't add dependencies whose license forbids
  that. Bundled dependencies keep their own licenses: list a new one in
  `THIRD_PARTY_NOTICES.md`. Lightweight Charts (Apache-2.0) asks for TradingView's
  notice and a link to tradingview.com where users see the charts; the status bar
  carries both (the charts set `attributionLogo: false`), so keep it visible,
  full screen included.
- **There is no login; the dashboard stays local.** Keep the default host
  `127.0.0.1`; a non-loopback host is refused unless `--insecure` (both servers). Cross-site
  writes to `/api` are refused (`same_site.py`, and `cross_site` in `wed-server`), so
  another web page can't drive the dashboard. Don't add endpoints that expose
  secrets or files outside `data/`.
- **Time zones are explicit.** Bar times come in the feed's clock (`XAU_CLOCK`; UTC for
  TradingView, a broker clock such as NY+7 for MT5 report times); the quarterly
  view works in New York time. Convert with `quarters.to_new_york` / `utc_to_feed` rather
  than assuming UTC.
- **Storage runs on SQLite and PostgreSQL.** Go through `storage.py` (SQLAlchemy); no
  SQLite-only SQL. The Rust server (`wed-store/src/lib.rs`) makes the same tables from
  `wed-store/schema.json`: after changing a table, run
  `UPDATE_RUST_SCHEMA=1 uv run pytest tests/test_rust_schema.py` and `make test-tauri`.
- **Optional extras stay optional.** `postgres` and `desktop` are extras; import them
  inside the function that needs them so the core runs without them, and report a missing one as an install hint.
- **Paid features live in the sibling `../wednesday-ee` plugin** (private repo),
  loaded through the `wednesday.plugins` entry point (`docs/plugins.md`). The core
  exposes hooks and feature ids; licence checks and paid code stay in the plugin, and
  the core must run the same without it. Users get it by entering a licence key:
  `plugin_install.py` downloads it from the licence server, checks its signature
  against `plugin_keys.py` and unpacks it to `data/plugins/`.

## Style

Match the surrounding code: type hints, dataclasses for settings with
`from_dict`/`to_dict`, short docstrings that say what and why. User-facing text (dashboard, docs, errors) is plain and
direct, and error messages say what to do next ("Set the bot token in Settings > Telegram alerts").
