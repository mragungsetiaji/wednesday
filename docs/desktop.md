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

`Wednesday-<version>-portable.zip` is the same program without an installer:
unzip it anywhere and run `Wednesday.exe`.

## Settings and data

Everything the app writes lives in `%LOCALAPPDATA%\Wednesday` (the Start menu has
a **Wednesday settings folder** shortcut to it), not next to the program, so
upgrading or uninstalling keeps it:

| Path | What |
| --- | --- |
| `.env` | Created from `.env.example` on first run. Put the MT5 login, Telegram token and LLM keys here, then restart the app. |
| `data\xau.db` | Settings saved from the dashboard and the M1 history |
| `data\models\` | Lab model files |
| `logs\screener.log` | The log, with every scan |
| `webview\` | The window's storage: dashboard layout and preferences |

The data source, alerts and the rest are set in the dashboard under **Settings**,
as in the browser version. The installer includes the MT5, news brief and Lab
extras, so every source and feature works without installing Python.

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
4. the installer with Inno Setup (`packaging/wednesday.iss`, output `dist\WednesdaySetup-<version>.exe`),
5. a portable zip of `dist\Wednesday\`.

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
