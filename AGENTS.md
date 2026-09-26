# AGENTS.md

Wednesday is a Smart Money Concepts screener for XAUUSD (gold): a Python scan loop
(FastAPI, SQLAlchemy) and a React + Vite + TypeScript dashboard in `web/`. A single
discretionary trader runs it, usually on a Windows VPS with MetaTrader 5, and acts on
its levels by hand. It suggests; it never places trades.

## Commands

```bash
make install     # uv sync --extra llm --extra ml, plus npm install in web/
make test        # uv run pytest -q
make typecheck   # tsc -b in web/
make demo        # dashboard on synthetic data, no market data or keys needed
make dev         # synthetic API + Vite hot reload on http://localhost:5173
make scan ARGS="--source synthetic"   # one scan in the console
```

Python is managed by uv (3.11 locally, `>=3.10` supported); use `uv run`, not a bare
`python`. Run `make test` after Python changes and `make typecheck` after changes in
`web/`. Tests and local runs should use `--source synthetic`: the other sources hit
Yahoo Finance or need an MT5 terminal, which only exists on Windows.

## Where things are

`docs/development.md` has the module map and how a scan flows; read it before a
change that crosses modules. Feature docs live in `docs/` (one file per area), and
`docs/detectors.md` / `docs/data-sources.md` explain how to add a detector or a data
source. When a change alters behaviour a user sees, update the matching doc and, for
the dashboard, its screenshot reference if the layout changed.

`src/xau_screener/` holds only stale `__pycache__` from the project's old name; the
package is `src/wednesday/`.

## Rules that matter here

- **Only closed candles.** Resampling drops the candle still forming and features in
  `lab/` are causal. A detector or feature that reads the forming bar, or any future
  bar, gives signals that look good in history and fail live.
- **The bias is the trader's call.** `bias.py` is set by hand; the LLM brief
  (`brief.py`) only offers a suggestion the trader applies with a click. Don't make
  anything set the bias, or trade, on its own.
- **Secrets never go into the database.** The MT5 password is entered in Settings
  and kept in the OS credential store (`keyring`, see `settings.py`); the Telegram
  token and LLM API keys are read from `.env`. None may be written to the database,
  logs, API responses or the dashboard. `.env` is gitignored; add new variables to
  `.env.example` with an empty value and a comment.
- **The dashboard has no login.** Keep the default host `127.0.0.1`, and don't add
  endpoints that expose secrets or files outside `data/`.
- **Time zones are explicit.** Bar times come in the feed's clock (`XAU_CLOCK`, often
  NY+7 on MT5 brokers); the quarterly view works in New York time. Convert with
  `quarters.to_new_york` / `utc_to_feed` rather than assuming UTC.
- **Storage runs on SQLite and PostgreSQL.** Go through `storage.py` (SQLAlchemy); no
  SQLite-only SQL.
- **Optional extras stay optional.** `MetaTrader5`, `anthropic`/`openai` and
  `scikit-learn`/`pyarrow` are extras; import them inside the function that needs
  them so the core runs without them, and report a missing one as an install hint
  (see `brief.py`).
- **Paid features live in the sibling `../wednesday-ee` plugin** (private repo),
  loaded through the `wednesday.plugins` entry point (`docs/plugins.md`). The core
  exposes hooks and feature ids; licence checks and paid code stay in the plugin, and
  the core must run the same without it.

## LLM news brief

`brief.py` calls Claude (default `claude-opus-5`, with server-side refusal fallbacks)
or OpenAI with the same prompt. The dashboard parses the reply's final
`BIAS: BULLISH|BEARISH|NEUTRAL` line, so a prompt change must keep that line. Tests
replace the model call through `brief_mod.ASK`; they never call a real API.

## Style

Match the surrounding code: type hints, dataclasses for settings with
`from_dict`/`to_dict`, short docstrings that say what and why. User-facing text (dashboard, docs, errors) is plain and
direct, and error messages say what to do next ("Set ANTHROPIC_API_KEY in .env and
restart").
