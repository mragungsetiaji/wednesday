# PyInstaller spec for the desktop app's server, the Tauri shell's sidecar (src-tauri, #92).
# Build from the repository root, after `npm run build` in web/, `npm ci --omit=dev` in tradingview/,
# node (Node 22) copied to packaging/node/ with its LICENSE, and
# `uv sync --extra desktop`:
#
#   uv run --with pyinstaller pyinstaller packaging/wednesday.spec --noconfirm
#
# Output: dist/wednesday-server/ (one folder, no window of its own), which
# src-tauri/tauri.release.conf.json bundles into the app.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent

if not (ROOT / "web" / "dist" / "index.html").is_file():
    raise SystemExit("Build the dashboard first: cd web && npm ci && npm run build")

# The TradingView data source: its Node helper and node.exe to run it (feeds.tradingview_command).
NODE = ROOT / "packaging" / "node"
NODE_EXE = "node.exe" if sys.platform == "win32" else "node"  # what feeds._bundled looks for
if not (ROOT / "tradingview" / "node_modules" / "@mathieuc" / "tradingview").is_dir():
    raise SystemExit("Install the TradingView helper first: cd tradingview && npm ci --omit=dev")
if not (NODE / NODE_EXE).is_file() or not (NODE / "LICENSE").is_file():
    raise SystemExit(f"Copy {NODE_EXE} (Node 22) and its LICENSE into packaging/node/")

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(ROOT / "DISCLAIMER.md"), "."),  # shown in the app until accepted (terms.py)
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),  # Lightweight Charts (Apache-2.0) and the other bundled packages
    (str(ROOT / "tradingview" / "feed.mjs"), "tradingview"),
    (str(ROOT / "tradingview" / "package.json"), "tradingview"),
    (str(ROOT / "tradingview" / "node_modules"), "tradingview/node_modules"),
    (str(NODE / "LICENSE"), "node"),
]
binaries = [(str(NODE / NODE_EXE), "node")]
# Time zone database for zoneinfo: Windows has none of its own.
datas += collect_data_files("tzdata")

# keyring finds its backends (Windows Credential Manager) through package metadata.
datas += copy_metadata("keyring")

# uvicorn picks its loop and protocol modules by name at runtime; so does keyring its backends.
hiddenimports = (collect_submodules("wednesday") + collect_submodules("uvicorn")
                 + collect_submodules("keyring.backends") + collect_submodules("win32ctypes"))
# Wednesday EE is downloaded after install (plugin_install.py) and can only import what is bundled
# here: the standard library modules it uses that the core might not, and all of cryptography.
hiddenimports += (["argparse", "ast", "platform", "secrets", "tempfile", "hashlib", "urllib.request",
                   "urllib.error", "zipfile"] + collect_submodules("cryptography"))

NAME = "wednesday-server"

a = Analysis(
    [str(ROOT / "packaging" / "server_launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=str(ROOT / "packaging" / "wednesday.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
