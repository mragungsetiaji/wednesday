# Running on Windows: local test and VPS production

The MetaTrader 5 Python API talks to a running MT5 terminal on the same
machine, in the same Windows user session. So the bot and the terminal always
live together: on your PC while testing, on the VPS in production.

## 1. Local test (Windows PC)

1. Install and log in to the MT5 terminal. Open **Market Watch** and note the
   exact gold symbol name (`XAUUSD`, `XAUUSD.m`, `GOLD`, ...).
2. In MT5: **Tools > Options > Charts > Max bars in chart** should be at least
   `100000` (the 4H scan needs about 48k M1 bars).
3. Install uv and the project:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   git clone <repo-url> wednesday
   cd wednesday
   uv sync --extra mt5 --extra llm --extra ml
   copy .env.example .env   # set XAU_SOURCE=mt5 and XAU_SYMBOL (credentials optional if MT5 is logged in)
   ```

   The default source is Yahoo Finance, so without `XAU_SOURCE=mt5` the screener
   runs on free futures data. You can also switch later in the dashboard under
   **Settings**; the choice is saved in the database and survives restarts.

4. Test the connection. It prints the account, symbol, bid/ask and how many M1
   bars were loaded:

   ```powershell
   uv run wednesday --source mt5 --check
   ```

   If it reports fewer M1 bars than needed, open an M1 chart of the symbol and
   scroll back (or press Home) so the terminal downloads more history, then retry.

5. One scan, then the live loop:

   ```powershell
   uv run wednesday --once
   uv run wednesday
   ```

## 2. Production (Windows VPS)

Do the same setup as above on the VPS (MT5, uv, `uv sync --extra mt5 --extra llm --extra ml`, `.env`,
`--check`). Then make it survive reboots and crashes:

1. **Auto-login the VPS user.** The MT5 API needs an interactive desktop
   session, so the bot cannot run as a Windows service in session 0. Enable
   automatic logon for the user (Sysinternals *Autologon*, or `netplwiz`).
2. **Start MT5 at logon.** Either put a shortcut to `terminal64.exe` in
   `shell:startup`, or set the terminal path, login and server in **Settings** (the password
   there too; it goes to Windows Credential Manager for the VPS user): the bot
   then launches and logs in to the terminal itself when it is not running.
   If that connection fails, or a lost one fails 3 times in a row, the feed
   stops rather than reopening the terminal; press **Reconnect** in Settings.
3. **Register the bot as a logon task** (run once, from the repo folder):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
   Start-ScheduledTask -TaskName "Wednesday"
   ```

   The task waits 60 s after logon, then runs `scripts\run_wednesday.ps1`,
   which restarts the screener 30 s after any exit. Inside the screener, a
   failed MT5 fetch triggers a reconnect, and any error just retries on the
   next minute.
4. **Disconnect RDP, don't sign out.** Close the Remote Desktop window (the
   session keeps running). *Signing out* ends the session and stops both MT5
   and the bot.

### Dashboard on the VPS

Build the dashboard once (on the VPS with Node 20+, or build on your PC and copy
the `web\dist` folder over):

```powershell
cd web; npm install; npm run build; cd ..
```

With `XAU_SERVE=1` in `.env`, the logon task serves it on port 8000. The
dashboard has **no login**, so keep `XAU_HOST=127.0.0.1` and reach it by one of:

- a browser inside the RDP session: `http://127.0.0.1:8000`
- an SSH tunnel from your PC (needs OpenSSH Server on the VPS):
  `ssh -L 8000:127.0.0.1:8000 user@vps`, then open `http://127.0.0.1:8000` locally

Only set `XAU_HOST=0.0.0.0` if the Windows firewall / VPS provider restricts
port 8000 to your own IP.

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
uv sync --extra mt5 --extra llm --extra ml
cd web; npm install; npm run build; cd ..
uv run wednesday --check
Start-ScheduledTask -TaskName "Wednesday"
```

## Timezone note

MT5 bar times are broker server time (often GMT+2/+3). The screener keeps that
clock, so 4H and 1H candles line up with the MT5 chart. Log timestamps use the
VPS clock.
