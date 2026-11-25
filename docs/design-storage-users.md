# Design: accounts and a market-data store

Status: **being built** on `design-storage-users`; steps 1 to 4 of the [build order](#build-order) are done.

Wednesday stays one trader's tool, used from several devices (PC and laptop). Two
additions to the Rust server (`wed-server`, `wed-store`), and the Python reference
follows both:

1. **Accounts in a `users` table, with a session per device.** Settings, drawings, bias
   and journals are already on the server, so every device sees the same data once
   logged in.
2. **A market-data store.** OHLCV bars per source, ticker and timeframe, for several
   tickers (FXCM and OANDA gold side by side), loaded only when a ticker is picked. TradingView
   provides the live data and every closed bar is ingested. Charts and the scan read
   history from the database first and only go to the source for what is missing.

## Where things stand (main, 0.4.1)

| Area | Today |
| --- | --- |
| Bars | `m1_bars (source, symbol, time)`. The feed asks TradingView for 1m only (`wed-engine/src/feed.rs`, `candles("1", …)`), and 5m–4H are resampled from it. A second symbol in a pane (`/api/markets/candles`) asks for its timeframe directly, but nothing is stored. |
| Depth | TradingView caps each series by bar count, not by time: roughly 6–7k bars without an account. Measured on 2026-11-25 for `FX:XAUUSD`: 1m gives about 5 days; native 4H pages back to 2023-01-02 (about 6,000 bars); native 1H gives 2,000 bars to 2026-07 in one page. Because the scan builds 4H from 1m, its 4H only reaches as far back as the stored 1m. |
| Login | None in Rust; the Python server reads one password from `XAU_AUTH_PASSWORD(_HASH)`. The Rust server refuses a non-loopback host unless `--insecure`; the preview runs behind nginx basic auth. |
| Schema | `wed-store/schema.json` is written from the Python reference (`storage.py`, `tests/test_rust_schema.py`). There is `CREATE TABLE IF NOT EXISTS` plus "add missing columns", and no versioned migrations. |

## Part 1: market-data store

### Tables

```sql
-- One row per closed candle. The forming candle is never stored (only closed candles).
CREATE TABLE bars (
  source  VARCHAR(32) NOT NULL,   -- 'tradingview', later others
  symbol  VARCHAR(64) NOT NULL,   -- 'FX:XAUUSD'
  tf      VARCHAR(8)  NOT NULL,   -- '1m','5m','15m','30m','1h','4h','1d' (the source's own candles)
  time    BIGINT      NOT NULL,   -- candle open, unix seconds, feed clock (UTC for TradingView)
  open    DOUBLE PRECISION NOT NULL,
  high    DOUBLE PRECISION NOT NULL,
  low     DOUBLE PRECISION NOT NULL,
  close   DOUBLE PRECISION NOT NULL,
  volume  DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (source, symbol, tf, time)
);

-- What is being ingested and how far each series reaches.
CREATE TABLE series (
  source     VARCHAR(32) NOT NULL,
  symbol     VARCHAR(64) NOT NULL,
  tf         VARCHAR(8)  NOT NULL,
  active     BOOLEAN     NOT NULL,  -- keep it live
  first_time BIGINT,                -- oldest stored bar
  last_time  BIGINT,                -- newest stored closed bar
  backfill   VARCHAR(16) NOT NULL,  -- 'pending' | 'running' | 'done' (the source has nothing older)
  updated_at VARCHAR(40) NOT NULL,
  error      TEXT,
  PRIMARY KEY (source, symbol, tf)
);
```

- **Only the source's own candles are stored.** Resampled candles are computed when read,
  so the table never holds two versions of one candle.
- **Times in the feed clock**, as `m1_bars` kept them: UTC for TradingView, so nothing
  changes for it.
- **`m1_bars` becomes `bars` with `tf = '1m'`.** Migration 1 (`schema_migrations`) copies it
  across and drops the old table.
- **PostgreSQL on the server, SQLite on the desktop.** Both keep working. On PostgreSQL,
  TimescaleDB is used when the extension is present: `bars` becomes a hypertable on
  `time` with compression after 30 days. Without the extension it is a plain table,
  which is fine at this size.

### Markets: several tickers, loaded when picked

The dropdown's list moves from `web/src/marketSymbol.ts` (`TICKERS`) into the database,
so the same instrument can be listed from several brokers, such as `FX:XAUUSD` (FXCM) and
`OANDA:XAUUSD`:

```sql
CREATE TABLE markets (
  source     VARCHAR(32)  NOT NULL,  -- 'tradingview'
  symbol     VARCHAR(64)  NOT NULL,  -- with the exchange prefix: 'FX:XAUUSD', 'OANDA:XAUUSD'
  label      VARCHAR(32)  NOT NULL,  -- what the menu shows: 'XAUUSD'
  name       VARCHAR(100),           -- 'Gold spot'
  venue      VARCHAR(32),            -- shown beside the label: 'FXCM', 'OANDA'
  position   INTEGER      NOT NULL,  -- order in the menu
  keep_live  BOOLEAN      NOT NULL,  -- keep ingesting when not selected (default false)
  added_at   VARCHAR(40)  NOT NULL,
  PRIMARY KEY (source, symbol)
);
```

- **Seeded** with today's list plus `OANDA:XAUUSD`. Settings > Markets adds a symbol
  (checked against TradingView's `resolve_symbol` before it is saved), renames it,
  reorders the list or removes it. The menu shows the label and the venue, so
  `XAUUSD · FXCM` and `XAUUSD · OANDA` are told apart.
- **Data is kept per ticker.** `bars`, `series` and `drawings` are already keyed by
  `(source, symbol)`, and the full symbol carries the broker, so FXCM and OANDA gold
  never mix.
- **Nothing loads until it is picked.** Listing a ticker fetches nothing. Picking it in
  the dropdown, or opening it in a pane, creates its `series` rows and starts the
  backfill and the live feed. The scan moves to the picked ticker, as today.
- **Leaving a ticker** stops its live feed unless `keep_live` is on. Picking it again
  fills the gap from `last_time`. TradingView's depth limits that refill: higher
  timeframes come back in full, but 1m is lost beyond about 5 days, so `keep_live` is
  there for a ticker whose 1m history matters.

### Every timeframe from its own series

Today a fresh install shows about 5 days of 4H and D1, because both are built from 1m and
TradingView gives about 5 days of 1m. TradingView caps each series by bar count, so the
coarser the timeframe the further back it reaches. The store therefore ingests every
timeframe the dashboard and scan use for the picked ticker, each as its own series, not
only the one on screen. Above 1m they are small (a few thousand rows each).

**Candles follow TradingView's own boundaries,** so the chart and the scan show the same
candles as TradingView itself. Measured for `FX:XAUUSD` on 2026-11-25:

| | 4H candles open at (UTC) |
| --- | --- |
| TradingView native 4H (used from now on) | 03, 07, 11, 15, 19, 23 (FX session: the day opens at 18:00 New York) |
| Today's resample from 1m (replaced) | 00, 04, 08, 12, 16, 20 |

- **The scan reads each timeframe's native series**: 5m, 15m, 30m, 1H, 4H and D1 as
  TradingView gives them, with no resampling from 1m. Each closed candle is the one
  TradingView closed. 4H with the default lookback (200 candles) is ready on first
  start, because native 4H reaches back years.
- **Each broker keeps its own sessions.** OANDA's 4H and daily boundaries may differ
  from FXCM's; every ticker uses its own.
- **This changes results:** 4H and D1 levels move to the new boundaries. The Python
  reference changes the same way (the scan takes bars per timeframe), and
  `tests/golden` is written again from it, so Rust and Python still agree.
- **1m stays** for the live candle, bar replay and the quarterly view. The forming candle
  of each timeframe is built in memory from ticks and is never stored or scanned (only
  closed candles).
- **Sources without native timeframes** (synthetic, a future CSV source) still resample
  from 1m. The resampler takes the session's start, so it can match TradingView's grid.

### Ingest

One ingest task in the server for each `series` row with `active = true`. A series is
active while its ticker is picked, open in a pane, or set to `keep_live`. Work runs in
priority order: the picked ticker's timeframes for the scan, then the one on screen, then
the rest.

1. **Backfill.** When `backfill` is not `done`, page older bars with
   `request_more_data` (wed-engine `tradingview.rs`) and upsert them in batches. Pause
   between pages so TradingView doesn't cut the connection. When a page returns nothing
   older, set `backfill = 'done'`.
2. **Live.** Keep the chart session open per symbol and timeframe. When a candle closes,
   upsert the closed candle. The forming candle stays in memory for the live price, as
   now.
3. **Gaps.** After a reconnect or a restart, fetch from `last_time` to now and upsert.
   Upserts on the primary key make repeated fetches harmless.
4. **Corrections.** A closed candle re-sent with different values overwrites the stored
   one, so the source's version wins.

Picking a ticker or opening it in a pane creates its `series` rows, so a second symbol in
a pane is stored too.

### Reading

`GET /api/candles` and `/api/markets/candles` (with or without `before`):

1. Read the timeframe's own series from `bars`.
2. If the range isn't stored and `backfill != 'done'`, fetch it from the source, upsert
   it and answer.
3. Add the forming candle from memory to the latest page only.

The scan starts from the database. A restart needs the network only to fill the gap.

### Size

| Series | Rows a year | Note |
| --- | --- | --- |
| 1m, one symbol | ~370k | The largest; compressed by Timescale after 30 days |
| 5m … 4h | ~75k → ~1.6k | Small |
| 1d | ~260 | |

Nothing is deleted.

## Part 2: accounts and login across devices

Accounts live in a `users` table, so nothing about a user sits in `.env` or the Docker
env file. There is one trader today, with a session per device. The table still allows
more users later without a schema change.

```sql
CREATE TABLE users (
  id            VARCHAR(36)  PRIMARY KEY,       -- uuid
  username      VARCHAR(64)  NOT NULL UNIQUE,   -- lowercased
  email         VARCHAR(254) UNIQUE,            -- optional, lowercased
  name          VARCHAR(100),
  password_hash VARCHAR(255) NOT NULL,          -- argon2id
  role          VARCHAR(16)  NOT NULL,          -- 'admin' (today the only one) | 'user'
  disabled      BOOLEAN      NOT NULL,
  created_at    VARCHAR(40)  NOT NULL,
  password_at   VARCHAR(40)  NOT NULL,          -- sessions from before this are void
  last_login_at VARCHAR(40)
);

CREATE TABLE sessions (
  token_hash   VARCHAR(64) PRIMARY KEY,         -- sha256 of the cookie value; the token itself is never stored
  user_id      VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device       VARCHAR(100),                    -- name set at login, or taken from the user agent
  created_at   VARCHAR(40) NOT NULL,
  expires_at   VARCHAR(40) NOT NULL,
  last_seen_at VARCHAR(40) NOT NULL
);
```

- **Data stays the trader's.** Settings, drawings, bias and journals don't get a
  `user_id` yet: with one trader every device sees the same data. If other users ever
  join, a migration adds `user_id` to those tables and assigns the existing rows to the
  first admin. That decision waits until it is needed.

### First account

On an empty `users` table nobody can log in, so the first account is created once:

- **Setup code (default).** At start with no users, the server prints a one-time code to
  its log (`docker logs wednesday`). The login page shows **Create account**, which asks
  for that code, a username and a password. The code works once and expires after an
  hour or at restart. A stranger who finds the page first can't claim the account.
- **CLI.** `wednesday-rs --create-user <username>` (in Docker: `docker exec -it
  wednesday wednesday-rs --create-user …`) reads the password from the terminal. This
  also resets a forgotten password.
- **Migration from today.** If `XAU_AUTH_PASSWORD` or `XAU_AUTH_PASSWORD_HASH` is set and
  `users` is empty, the server creates `admin` with that password once and logs that
  the variable can now be removed.

### Login

- **Password:** argon2id. A scrypt hash migrated from `XAU_AUTH_PASSWORD_HASH` is
  accepted and rehashed to argon2id at the next login.
- **Cookie:** HttpOnly, SameSite=Strict, Secure over https. Sessions last 30 days and
  are renewed as they are used.
- **Settings > Account:** change the password (logs out every other device), plus a list
  of devices with a logout button for each (for example a lost laptop).
- **Login rate limit:** for example 10 attempts per IP and per username in 15 minutes.
- **Route guard:** a middleware on `/api` refuses without a session, except
  `/api/health`, `/api/auth/login`, `/api/auth/setup` and `/api/auth/status`. Port
  `tests/test_auth.py`, which walks every route, so new routes are guarded by default.
- **When the login is on:** always on a non-loopback host; `--insecure` stays for
  development only. The desktop app on loopback runs as now, with no login. Once the
  login works, the nginx basic auth on the preview can go.
- **Devices see each other's changes.** They share the database. A change from another
  device shows on the next load; live push is a later step.

### Secrets

A hash is one-way. It fits a password, which is only ever compared, but not the Telegram
bot token or an LLM API key, which the server must send in plain text to Telegram or
the API. Those stay as now: OS credential store, or `.env` / the Docker env file on a
server, never the database. With one trader there is no need for per-user keys.

## Migrations

Versioned migrations replace "create if missing":

- A `schema_migrations (version, applied_at)` table, and ordered migrations in
  `wed-store/migrations/NNNN_name.{sqlite,postgresql}.sql`.
- Each migration runs inside a transaction at startup.

The Python reference (`storage.py`) gets the same tables and migrations, and
`schema.json` is still checked against it.

## Build order

1. Migration runner, `bars`, `series` and `markets`, plus copying `m1_bars` over
   (Rust and Python).
2. Read path from `bars`, so the scan and charts open from the database.
3. Ingest per timeframe for the picked ticker: backfill, live closes, gap fill,
   priority order, stop on leaving unless `keep_live`.
4. The scan on native timeframes (Rust and Python). `tests/golden` stays as it is: the
   synthetic source has no timeframes of its own, so the golden scans still resample M1.

Done: steps 1 to 4. A gap longer than one page (5,000 candles) between the stored and
the fetched candles isn't filled yet; the backfill only reaches back from the first
stored candle.
5. The dropdown from `markets`, with the venue shown, and Settings > Markets.
6. `users` and `sessions`, the setup code and `--create-user`, route guard, login page,
   Settings > Account.
7. Settings > Data: ingest status per series (from `series`).

## Open questions

1. **TradingView account:** logging in to TradingView raises the bar cap, which gives
   more 1m depth. Its credentials would be a server-wide secret (credential store or
   `.env`).
