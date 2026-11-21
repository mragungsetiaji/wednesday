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
   uv sync
   copy .env.example .env
   ```

   The data source is TradingView (spot gold, `FX:XAUUSD`); it needs nothing
   installed. Change the symbol in
   `.env` (`XAU_SYMBOL`, with `XAU_SOURCE=tradingview`) or later in the dashboard
   under **Settings**; the choice is saved in the database and survives restarts.
   See [Data sources](data-sources.md).

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

Do the same setup as above on the VPS (uv, `uv sync`, `.env`,
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

With `XAU_SERVE=1` in `.env`, the logon task serves it on port 8000. The
dashboard has no login, so keep `XAU_HOST=127.0.0.1` and reach it by one of:

- a browser inside the RDP session: `http://127.0.0.1:8000`
- an SSH tunnel from your PC (needs OpenSSH Server on the VPS):
  `ssh -L 8000:127.0.0.1:8000 user@vps`, then open `http://127.0.0.1:8000` locally
- a VPN to the VPS (WireGuard, Tailscale), with Wednesday bound to the VPN address
  and `--insecure`

Wednesday refuses to start on any other address unless you pass `--insecure`:
anyone who reaches the port could change the data source, set the bias and
delete data. Never open the port to the internet. State-changing
requests from another web site are refused either way.

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
uv sync
cd web; npm install; npm run build; cd ..
uv run wednesday --check
Start-ScheduledTask -TaskName "Wednesday"
```

## Timezone note

TradingView bar times are UTC, and the screener keeps the
feed's clock (see [Feed clock](data-sources.md#feed-clock)); the quarterly view
converts to New York time. Log timestamps use the VPS clock.
