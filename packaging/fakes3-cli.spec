# PyInstaller spec — fakes3-cli.exe (console, onefile)
# Build: python -m PyInstaller --noconfirm packaging/fakes3-cli.spec

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "cli_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # uvicorn selects these at runtime via importlib
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
    ],
    excludes=[
        "PySide6", "shiboken6",           # GUI stays out of the CLI build
        "tkinter", "boto3", "botocore", "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="fakes3-cli",
    icon=str(ROOT / "packaging" / "icons" / "fakes3.ico"),
    console=True,
    upx=False,
    strip=False,
)
