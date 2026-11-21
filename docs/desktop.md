# Windows desktop app

Wednesday also ships as a Windows program: an installer that puts it in the Start
menu and on the desktop, and opens the dashboard in its own window instead of a
browser tab. It runs Wednesday's server (the scan loop and the dashboard) in its
own process; closing the window stops both.

## Install

1. Download `WednesdaySetup-<version>.exe` from the
   [releases page](https://github.com/mragungsetiaji/wednesday/releases).
2. Run it. It installs for your Windows user only, so it asks for no admin rights.
   Windows SmartScreen may say "Windows protected your PC" because the installer
   is not code signed: click **More info > Run anyway**.
3. Start **Wednesday** from the Start menu or the desktop icon.

### Upgrading

Run the new installer over the old one. Settings, the database, journals, logs
and the saved secrets stay where they are.

From 0.3.1 the app is built with Tauri (see below). Its installer first removes
the earlier build (0.3.0 and before) if it finds it, closing it if it is
running, so only one Wednesday stays installed; that build's uninstaller keeps
`%LOCALAPPDATA%\Wednesday` and the secrets. The dashboard's own layout
preferences (panel sizes, the chart's tools) start from the defaults once;
settings, drawings and workspaces are in the database and carry over.

To start from scratch, uninstall Wednesday and tick **Delete the application
data**: that also deletes `%LOCALAPPDATA%\Wednesday` and the secrets saved in
Windows Credential Manager. Or run the installer with `/FRESH=1`
(`WednesdaySetup-<version>.exe /FRESH=1`), which does the same after installing.
This can't be undone.

The window is drawn by Microsoft Edge WebView2. Windows 11 has it; if a Windows 10
PC lacks it, the installer downloads it.

## Settings and data

Everything the app writes lives in `%LOCALAPPDATA%\Wednesday` (type it in the
Explorer address bar), not next to the program, so upgrading or uninstalling
keeps it. The Tauri shell keeps its window positions and the dashboard's layout
preferences in `%LOCALAPPDATA%\app.wednesday.desktop`.

| Path | What |
| --- | --- |
| `data\xau.db` | Settings saved from the dashboard and the M1 history |
| `logs\screener.log` | The server's log (starts over past 5 MB) |
| `webview\` | The 0.3.0-and-earlier window's storage, no longer used |

Everything is set in the dashboard under **Settings**; the app needs no `.env`.
The Telegram bot token is kept in Windows
Credential Manager (entries named `Wednesday`), never in the database or a file. A `.env` in this folder is still read if you create one.

On first start the app shows the [disclaimer and risk agreement](../DISCLAIMER.md);
it can't be used until you accept it.
A chart can open in its own window, for another monitor: the pop-out button
in the chart's top bar (see [Charts in their own windows](dashboard.md#charts-in-their-own-windows)).
Those windows use the same local server and close with the main window.

The app remembers its windows: when it closes, each window's position, size,
maximised state and chart are kept, and the next start puts them back. A window
whose monitor is gone (a laptop undocked) opens on the main screen, fully
visible. Named workspaces (see [Workspaces](dashboard.md#workspaces)) switch
between saved window sets.

The installer includes Node.js with the
TradingView helper, so everything works without installing Python, Rust or
Node.

## Data source

The data source is TradingView (spot gold, `FX:XAUUSD` by default). In
**Settings > Data source** you can change the symbol, the feed clock and the live
price interval; see [Data sources](data-sources.md). **Save and restart
feed** applies it. The status says **Connecting…** until the first scan is in,
and only then **Connected**.

MetaTrader 5, Yahoo Finance and CSV files are no longer data sources
(connecting to MT5 opened the terminal when the app started and froze it). If an
older version was set to one of them, the app starts on TradingView instead.
The [journal](journal.md) still imports MT5's history report. Price history is
what the TradingView feed fetches and the app stores while it runs; there is no
import of M1 files.

Only one copy runs at a time; starting a second one brings the first to the front.

`WEDNESDAY_HOME` points the app at another settings folder (default
`~/.wednesday` outside Windows).

## How it's built

The app is a [Tauri v2](https://tauri.app) shell (#92, `src-tauri/`); it replaced
the pywebview app in 0.3.1. Since #96 it runs Wednesday's Rust server (`wed-server/`) in
its own process, with no Python: the scan engine (the TradingView connection, the
minute scans, the live price), the alerts, every route the dashboard calls and the
dashboard build, on `127.0.0.1` (port 8000 when free). It shows the dashboard in
native windows. Pop-out charts are Tauri windows that can sit on any monitor. Where
the windows were when the app closed is kept in the shell's own data folder
(`windows.json`) and restored on the next start; a window left on an unplugged
monitor comes back onto the first one. The server stops with the app.

The installer's resources hold what the server needs besides itself
(`tauri.release.conf.json`). TradingView is reached over its websocket by the server, so
neither Node.js nor a helper ships with the app:

| Path | What |
| --- | --- |
| `web\` | The dashboard build (`web/dist`) |
| `LICENSE`, `DISCLAIMER.md`, `THIRD_PARTY_NOTICES.md` | The license and notices |

`Wednesday.exe --forget-secrets` removes the secrets Wednesday saved in the
credential store and exits without a window; the installer's fresh start runs it
(`src-tauri/windows/hooks.nsh`).

```bash
make tauri        # the app from this checkout (its web/dist)
make test-tauri   # its Rust tests, and the Rust server's
```

It needs Rust 1.77.2 or newer. `WEDNESDAY_HOME`, `XAU_SOURCE=synthetic` and
`XAU_UI_DIR` work as for the installed app. In a debug build, `WEDNESDAY_SELFTEST=1` makes the dashboard
call the shell's commands (open a chart window, read and arrange the windows) and
print the result; `WEDNESDAY_SELFTEST=channel` checks that `BroadcastChannel`, which
link groups and drawings use, reaches another window.

The installer's hooks (`src-tauri/windows/hooks.nsh`) remove the earlier Inno
Setup build before installing, and do the fresh start described under
[Upgrading](#upgrading).

## Build and release

`.github/workflows/release-windows.yml` builds on a Windows runner:

1. the dashboard (`npm ci && npm run build` in `web/`),
2. the Tauri app, with the Rust server built in, and its NSIS installer
   (`cargo tauri build --config tauri.release.conf.json` in `src-tauri/`, which
   bundles the dashboard as a resource), copied to
   `dist\WednesdaySetup-<version>.exe`.

To publish a release, tag main and push the tag; the version comes from the tag:

```bash
make release v0.3.2
```

It checks that you're on main with nothing uncommitted, in step with `origin/main`,
and that the tag is new and higher than main's version, then tags and pushes
(`scripts/release.sh`; the same as `git tag -a v0.3.2 && git push origin v0.3.2`).

The installer is attached to a GitHub release named after the tag.
Once the build passes, the workflow commits `Version 0.2.0` to `main`: the tag's
version written into every file that carries it (`pyproject.toml`,
`src/wednesday/__init__.py`, `uv.lock`, `web/package.json` and
`web/package-lock.json`, and the Tauri shell's `tauri.conf.json`, `Cargo.toml`
and `Cargo.lock`), so running from source shows it too. It never moves the
version backwards. To set it by hand, or check the files agree:

```bash
python scripts/set_version.py 0.2.0
python scripts/set_version.py --check
```

`tests/test_version.py` fails when the files disagree.
**Actions > Release Windows > Run workflow** builds without releasing; the files
are kept as the run's artifact.

The same steps on a Windows PC, from the repository root:

```powershell
cd web; npm ci; npm run build; cd ..
cargo install tauri-cli --version "^2" --locked
cd src-tauri; cargo tauri build --config tauri.release.conf.json
```

Build the installer on Windows: the NSIS bundle comes from there.
