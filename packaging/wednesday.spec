# PyInstaller spec for the Windows desktop app. Build on Windows from the repository root,
# after `npm run build` in web/ and `uv sync --extra desktop --extra mt5 --extra llm --extra ml`:
#
#   uv run --with pyinstaller pyinstaller packaging/wednesday.spec --noconfirm
#
# Output: dist/Wednesday/Wednesday.exe (one folder; packaging/wednesday.iss turns it into an installer).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

if not (ROOT / "web" / "dist" / "index.html").is_file():
    raise SystemExit("Build the dashboard first: cd web && npm ci && npm run build")

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(ROOT / ".env.example"), "."),
]
# Time zone database for zoneinfo: Windows has none of its own.
datas += collect_data_files("tzdata")

# uvicorn picks its loop and protocol modules by name at runtime.
hiddenimports = collect_submodules("wednesday") + collect_submodules("uvicorn")

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
