# Running on Windows: local test and VPS production

This page runs Wednesday from source, for a VPS that keeps it scanning around the
clock. On a desktop PC the [Windows app](desktop.md) is simpler: an installer, no
Python or Node to set up.

## 1. Local test (Windows PC)

1. Install uv and the project:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   git clone <repo-url> wednesday
   cd wednesday
   uv sync --extra llm --extra ml
   copy .env.example .env
   ```

   The default source is Yahoo Finance (COMEX futures, `GC=F`). For spot gold
   from TradingView, install Node 20+ and the helper (`npm install --prefix
   tradingview`), then set `XAU_SOURCE=tradingview` in `.env`. You can also
   switch later in the dashboard under **Settings**; the choice is saved in the
   database and survives restarts. See [Data sources](data-sources.md).

2. Test the connection. It prints the feed details, the last price and how many M1
   bars were loaded:

   ```powershell
   uv run wednesday --check
   ```

3. One scan, then the live loop:

   ```powershell
   uv run wednesday --once
   uv run wednesday
   ```

## 2. Production (Windows VPS)

Do the same setup as above on the VPS (uv, `uv sync --extra llm --extra ml`, `.env`,
`--check`). Then make it survive reboots and crashes:

1. **Auto-login the VPS user.** The task below starts at logon of that user, so
   enable automatic logon (Sysinternals *Autologon*, or `netplwiz`).
2. **Register the bot as a logon task** (run once, from the repo folder):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
   Start-ScheduledTask -TaskName "Wednesday"
   ```

   The task waits 60 s after logon, then runs `scripts\run_wednesday.ps1`,
   which restarts the screener 30 s after any exit. Inside the screener, any
   error just retries on the next minute.
3. **Disconnect RDP, don't sign out.** Close the Remote Desktop window (the
   session keeps running). *Signing out* ends the session and stops the bot.

### Dashboard on the VPS

Build the dashboard once (on the VPS with Node 20+, or build on your PC and copy
the `web\dist` folder over):

```powershell
cd web; npm install; npm run build; cd ..
```

With `XAU_SERVE=1` in `.env`, the logon task serves it on port 8000. Without a
login, keep `XAU_HOST=127.0.0.1` and reach it by one of:

- a browser inside the RDP session: `http://127.0.0.1:8000`
- an SSH tunnel from your PC (needs OpenSSH Server on the VPS):
  `ssh -L 8000:127.0.0.1:8000 user@vps`, then open `http://127.0.0.1:8000` locally

Wednesday refuses to start on any other address without a login (unless you pass
`--insecure`): anyone who reaches the port could change the data source, set the
bias, run LLM calls and delete models.

#### Opening it from a phone or laptop: login + HTTPS

1. Turn the login on. Either put the password in `.env`:

   ```ini
   XAU_AUTH_PASSWORD=a long passphrase
   ```

   or keep only its hash there: run `uv run wednesday --hash-password`, type the
   password twice, and paste the printed `XAU_AUTH_PASSWORD_HASH=scrypt$...` line
   into `.env`. The password never goes in the database; sessions and API tokens
   are stored as SHA-256 hashes only. Five wrong passwords from one address lock
   it out for five minutes.

2. Keep Wednesday on loopback and put [Caddy](https://caddyserver.com/) in front
   for HTTPS (it gets and renews the certificate itself). Point a DNS name at the
   VPS, open ports 80 and 443 in the firewall, and use this `Caddyfile`:

   ```caddy
   wednesday.example.com {
       encode gzip
       reverse_proxy 127.0.0.1:8000
   }
   ```

   Run it with `caddy run --config Caddyfile` (or install it as a service with
   `caddy.exe` + NSSM / a scheduled task like Wednesday's). Caddy passes the
   original `Host` and `X-Forwarded-Proto`, so the session cookie is marked
   `Secure` and same-site checks work.

   Binding Wednesday itself to `0.0.0.0` also works once the login is on, but then
   the password crosses the network in plain HTTP; use a proxy with HTTPS.

The session cookie is HttpOnly and SameSite=Strict, and state-changing requests
from another site are refused (with or without a login). Scripts use an API token
from **Settings > Access** (`Authorization: Bearer wed_...`); tokens can be
revoked there. `GET /api/health` is the one route open without a login, for
uptime checks.

Settings and every stored M1 bar live in `data\xau.db` (SQLite). Back that file
up with the rest of the folder; deleting it only means the history is fetched
again. To use PostgreSQL instead, see [Data sources and storage](data-sources.md#storage).

Logs go to `logs/screener.log` (rotated at 5 MB, 5 files kept) when
`XAU_LOG_FILE` is set; every scan table is written there too.

Useful commands on the VPS:

```powershell
Get-Content logs\screener.log -Tail 50 -Wait        # follow the log
Get-ScheduledTask -TaskName "Wednesday" | Get-ScheduledTaskInfo
Stop-ScheduledTask -TaskName "Wednesday"
Unregister-ScheduledTask -TaskName "Wednesday"   # remove
```

## Updating the VPS

```powershell
Stop-ScheduledTask -TaskName "Wednesday"
# if a python.exe from the bot is still alive, stop it (check the path before killing)
Get-Process python -ErrorAction SilentlyContinue | Where-Object Path -like "*wednesday*" | Stop-Process
git pull
uv sync --extra llm --extra ml
cd web; npm install; npm run build; cd ..
uv run wednesday --check
Start-ScheduledTask -TaskName "Wednesday"
```

## Timezone note

Yahoo Finance and TradingView bar times are UTC, and the screener keeps the
feed's clock (see [Feed clock](data-sources.md#feed-clock)); the quarterly view
converts to New York time. Log timestamps use the VPS clock.
