# Windows desktop app

Wednesday also ships as a Windows program: an installer that puts it in the Start
menu and on the desktop, and opens the dashboard in its own window instead of a
browser tab. It runs the same scan loop and dashboard as `wednesday --serve`;
closing the window stops both.

## Install

1. Download `WednesdaySetup-<version>.exe` from the
   [releases page](https://github.com/mragungsetiaji/wednesday/releases).
2. Run it. It installs for your Windows user only, so it asks for no admin rights.
   Windows SmartScreen may say "Windows protected your PC" because the installer
   is not code signed: click **More info > Run anyway**.
3. Start **Wednesday** from the Start menu or the desktop icon.

### Upgrading

Run the new installer over the old one. Settings, the database, journals, Lab
models, logs and the saved secrets stay where they are.

From 0.3.1 the app is built with Tauri (see below). Its installer first removes
the earlier build (0.3.0 and before) if it finds it, closing it if it is
running, so only one Wednesday stays installed; that build's uninstaller keeps
`%LOCALAPPDATA%\Wednesday` and the secrets. The dashboard's own layout
preferences (panel sizes, the chart's tools) start from the defaults once;
settings, drawings and workspaces are in the database and carry over.

To start from scratch, uninstall Wednesday, delete `%LOCALAPPDATA%\Wednesday`,
and remove the `Wednesday` entries in Windows Credential Manager.

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
| `data\models\` | Lab model files |
| `logs\screener.log` | The log, with every scan |
| `webview\` | The 0.3.0-and-earlier window's storage, no longer used |

Everything is set in the dashboard under **Settings**; the app needs no `.env`.
The Telegram bot token and the LLM API keys are kept in Windows
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

The installer includes the news brief and Lab extras, and Node.js with the
TradingView helper, so every source and feature works without installing Python
or Node.

## Data source

In **Settings > Data source**, pick Yahoo Finance (the default), TradingView,
a CSV file or demo data; see [Data sources](data-sources.md). **Save and restart
feed** applies it. The status says **Connecting…** until the first scan is in,
and only then **Connected**.

MetaTrader 5 is no longer a data source: connecting to it opened the terminal
when the app started and froze it. If an older version was set to MT5, the app
starts on Yahoo Finance instead; pick the source you want in Settings. The
[journal](journal.md) still imports MT5's history report, and
[Lab > Data](lab.md#data) imports M1 bars exported from MT5.

Only one copy runs at a time; starting a second one says it is already running.
The dashboard uses port `XAU_PORT` (8000) on `127.0.0.1`, or a free port when that
one is taken.

## Run from source

```bash
uv sync --extra desktop --extra llm --extra ml
make ui
uv run wednesday-desktop
```

`WEDNESDAY_HOME` points the app at another settings folder (default
`~/.wednesday` outside Windows).

## Tauri shell

The Windows app is built with [Tauri v2](https://tauri.app) (#92); it replaced
the pywebview build in 0.3.1. The shell lives in `src-tauri/`. The Python server
still does the work:
the shell starts it as a sidecar (`wednesday-server`, same settings folder, lock and
`.env`), waits for it to answer, and shows the dashboard in native windows. Pop-out
charts are Tauri windows that can sit on any monitor. Where the windows were when
the app closed is kept in the shell's own data folder (`windows.json`) and restored
on the next start; a window left on an unplugged monitor comes back onto the
first one. The server stops with the app, and by itself if the shell crashes.

```bash
make tauri        # the shell, starting `uv run wednesday-server` from this checkout
make test-tauri   # its Rust tests
```

It needs Rust 1.77.2 or newer. `WEDNESDAY_HOME` and `XAU_SOURCE=synthetic` work as
for the pywebview app. In a debug build, `WEDNESDAY_SELFTEST=1` makes the dashboard
call the shell's commands (open a chart window, read and arrange the windows) and
print the result; `WEDNESDAY_SELFTEST=channel` checks that `BroadcastChannel`, which
link groups and drawings use, reaches another window.

The installer's hooks (`src-tauri/windows/hooks.nsh`) remove the earlier Inno
Setup build before installing.

## Build and release

`.github/workflows/release-windows.yml` builds on a Windows runner:

1. the dashboard (`npm ci && npm run build` in `web/`),
2. the TradingView helper (`npm ci --omit=dev` in `tradingview/`) and `node.exe`
   with its license, copied into `packaging\node\`,
3. the Python environment (`uv sync --frozen --extra desktop --extra llm --extra ml`),
4. the server with PyInstaller (`WEDNESDAY_BUILD=server packaging/wednesday.spec`,
   output `dist\wednesday-server\`),
5. the Tauri shell and its NSIS installer (`cargo tauri build --config
   tauri.release.conf.json` in `src-tauri/`, which bundles the server folder),
   copied to `dist\WednesdaySetup-<version>.exe`.

To publish a release, tag a commit and push the tag; the version comes from the
tag:

```bash
git tag v0.3.1
git push origin v0.3.1
```

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
npm ci --omit=dev --prefix tradingview
New-Item -ItemType Directory -Force packaging\node
Copy-Item (Get-Command node).Source packaging\node\node.exe   # Node 22
Copy-Item (Join-Path (Split-Path (Get-Command node).Source) LICENSE) packaging\node\LICENSE
uv sync --frozen --extra desktop --extra llm --extra ml
$env:WEDNESDAY_BUILD = "server"
uv run --with pyinstaller pyinstaller packaging/wednesday.spec --noconfirm
cargo install tauri-cli --version "^2" --locked
cd src-tauri; cargo tauri build --config tauri.release.conf.json
```

PyInstaller can't cross-compile: the Windows build has to run on Windows. The
installed program is roughly 400 MB, mostly pandas, scikit-learn and pyarrow; the
installer is compressed to a fraction of that.
