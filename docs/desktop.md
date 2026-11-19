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

### Reinstalling over an earlier install

When `%LOCALAPPDATA%\Wednesday` already exists (Wednesday was installed on this
PC before), the installer asks what to do with it:

- **Keep my data and settings** (the default): settings, the database, journals,
  Lab models and logs stay as they are. This is the normal upgrade.
- **Fresh install: delete all previous data and settings**: asks you to confirm,
  then deletes that folder and removes the secrets Wednesday saved in Windows
  Credential Manager (API keys and the Telegram bot token, and the MT5 password
  older versions kept there). Journals, Lab models and stored price history go
  too, and it can't be undone. Close Wednesday first: files still in use can't be
  deleted, and the installer says so.

A silent install (`/SILENT` or `/VERYSILENT`) keeps the data; add `/FRESH=1` for
a fresh install.

The window is drawn by Microsoft Edge WebView2. Windows 11 has it; if a Windows 10
PC lacks it, the installer offers to download it.

## Settings and data

Everything the app writes lives in `%LOCALAPPDATA%\Wednesday` (the Start menu has
a **Wednesday settings folder** shortcut to it), not next to the program, so
upgrading or uninstalling keeps it:

| Path | What |
| --- | --- |
| `data\xau.db` | Settings saved from the dashboard and the M1 history |
| `data\models\` | Lab model files |
| `logs\screener.log` | The log, with every scan |
| `webview\` | The window's storage: dashboard layout and preferences |

Everything is set in the dashboard under **Settings**; the app needs no `.env`.
The Telegram bot token and the LLM API keys are kept in Windows
Credential Manager (entries named `Wednesday`), never in the database or a file. A `.env` in this folder is still read if you create one.

On first start the app shows the [disclaimer and risk agreement](../DISCLAIMER.md);
it can't be used until you accept it. The installer asks the same before
installing.
A chart can open in its own window, for another monitor: the pop-out button
under the chart (see [Charts in their own windows](dashboard.md#charts-in-their-own-windows)).
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

## Tauri shell (in progress)

The desktop app is moving from pywebview to [Tauri v2](https://tauri.app) (#92).
The new shell lives in `src-tauri/`. For now the Python server still does the work:
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

**Actions > Tauri Windows > Run workflow** builds a test installer: the server with
PyInstaller (`WEDNESDAY_BUILD=server`, output `dist\wednesday-server\`), then the
shell with `cargo tauri build --config tauri.release.conf.json`, which bundles
that folder into an NSIS installer. Releases still come from the pywebview build
below until the Tauri one has been tried on real machines. The Tauri shell keeps
the dashboard's own layout preferences in its own WebView storage, so they start
from the defaults once; settings, drawings and workspaces are in the database and
carry over.

## Build and release

`.github/workflows/release-windows.yml` builds on a Windows runner:

1. the dashboard (`npm ci && npm run build` in `web/`),
2. the TradingView helper (`npm ci --omit=dev` in `tradingview/`) and `node.exe`
   with its license, copied into `packaging\node\`,
3. the Python environment (`uv sync --frozen --extra desktop --extra llm --extra ml`),
4. the program with PyInstaller (`packaging/wednesday.spec`, output `dist\Wednesday\`),
5. the installer with Inno Setup (`packaging/wednesday.iss`, output `dist\WednesdaySetup-<version>.exe`).

To publish a release, tag a commit and push the tag; the version comes from the
tag:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The installer and the zip are attached to a GitHub release named after the tag.
Once the build passes, the workflow commits `Version 0.2.0` to `main`: the tag's
version written into every file that carries it (`pyproject.toml`,
`src/wednesday/__init__.py`, `uv.lock`, `web/package.json` and
`web/package-lock.json`), so running from source shows it too. It never moves the
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
uv run --with pyinstaller pyinstaller packaging/wednesday.spec --noconfirm
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=0.2.0 packaging\wednesday.iss
```

PyInstaller can't cross-compile: the Windows build has to run on Windows. The
installed program is roughly 400 MB, mostly pandas, scikit-learn and pyarrow; the
installer is compressed to a fraction of that.
