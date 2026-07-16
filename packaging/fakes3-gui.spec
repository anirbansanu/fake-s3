# PyInstaller spec — fakes3-gui.exe (windowed, onefile)
# Build: python -m PyInstaller --noconfirm packaging/fakes3-gui.spec

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "gui_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
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
        "tkinter", "boto3", "botocore", "pytest",
        # Qt modules the app does not use — keeps the exe smaller
        "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtConcurrent",
        "PySide6.QtDBus", "PySide6.QtPdf", "PySide6.QtSvg",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="fakes3-gui",
    icon=str(ROOT / "packaging" / "icons" / "fakes3.ico"),
    console=False,
    upx=False,
    strip=False,
)
