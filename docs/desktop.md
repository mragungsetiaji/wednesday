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
The MT5 password, the Telegram bot token and the LLM API keys are kept in Windows
Credential Manager (entries named `Wednesday` and `Wednesday MT5`), never in the
database or a file. A `.env` in this folder is still read if you create one.

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

The installer includes the MT5, news brief and Lab extras, so every source and
feature works without installing Python.

## Connecting to MetaTrader 5

In **Settings > Data source**, pick **MetaTrader 5**, then:

- **Terminal**: choose it from the list of MT5 terminals installed on this PC
  (MetaQuotes' own and each broker's), or **Browse…** to its `terminal64.exe`,
  or paste the path or its folder. With several terminals, pick the one logged in
  to the account you want.
- **Login**, **Server**, **Password**: optional when that terminal is already open
  and logged in. Paste the account number and the broker's server name as MT5
  shows them. The password is saved in Windows Credential Manager for that login,
  never in the database or a file; leave it empty later to keep it, or
  **Forget the saved password**.

**Save and restart feed** starts the terminal once if it isn't running. The status
says **Connecting…** until the first scan is in, and only then **Connected**. If
it can't connect (wrong password, you closed the login window, symbol missing),
it shows the error and stops: it doesn't open the terminal again. Fix the cause
and press **Reconnect**.

If the connection drops later, it tries again up to 3 times, a minute apart
(**Trying to reconnect, attempt 1 of 3**), only while the terminal is still open.
A terminal you closed is never reopened; after the third try the feed stops and
waits for **Reconnect**.

Only one copy runs at a time; starting a second one says it is already running.
The dashboard uses port `XAU_PORT` (8000) on `127.0.0.1`, or a free port when that
one is taken.

## Run from source

```bash
uv sync --extra desktop --extra llm --extra ml   # add --extra mt5 on Windows
make ui
uv run wednesday-desktop
```

`WEDNESDAY_HOME` points the app at another settings folder (default
`~/.wednesday` outside Windows).

## Build and release

`.github/workflows/release-windows.yml` builds on a Windows runner:

1. the dashboard (`npm ci && npm run build` in `web/`),
2. the Python environment (`uv sync --frozen --extra desktop --extra mt5 --extra llm --extra ml`),
3. the program with PyInstaller (`packaging/wednesday.spec`, output `dist\Wednesday\`),
4. the installer with Inno Setup (`packaging/wednesday.iss`, output `dist\WednesdaySetup-<version>.exe`).

To publish a release, tag a commit and push the tag; the version comes from the
tag:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The installer and the zip are attached to a GitHub release named after the tag.
**Actions > Release Windows > Run workflow** builds without releasing; the files
are kept as the run's artifact.

The same steps on a Windows PC, from the repository root:

```powershell
cd web; npm ci; npm run build; cd ..
uv sync --frozen --extra desktop --extra mt5 --extra llm --extra ml
uv run --with pyinstaller pyinstaller packaging/wednesday.spec --noconfirm
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=0.2.0 packaging\wednesday.iss
```

PyInstaller can't cross-compile: the Windows build has to run on Windows. The
installed program is roughly 400 MB, mostly pandas, scikit-learn and pyarrow; the
installer is compressed to a fraction of that.
