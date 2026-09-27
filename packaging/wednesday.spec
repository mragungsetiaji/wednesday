# PyInstaller spec for the Windows desktop app. Build on Windows from the repository root,
# after `npm run build` in web/ and `uv sync --extra desktop --extra mt5 --extra llm --extra ml`:
#
#   uv run --with pyinstaller pyinstaller packaging/wednesday.spec --noconfirm
#
# Output: dist/Wednesday/Wednesday.exe (one folder; packaging/wednesday.iss turns it into an installer).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent

if not (ROOT / "web" / "dist" / "index.html").is_file():
    raise SystemExit("Build the dashboard first: cd web && npm ci && npm run build")

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(ROOT / "DISCLAIMER.md"), "."),  # shown in the app until accepted (terms.py)
    (str(ROOT / "LICENSE"), "."),
]
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

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
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
    name="Wednesday",
    icon=str(ROOT / "packaging" / "wednesday.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Wednesday", upx=False)
