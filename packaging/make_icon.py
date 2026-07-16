"""Generate packaging/icons/fakes3.ico from the programmatic app icon."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes3.gui.resources import make_icon  # noqa: E402


def main() -> None:
    app = QApplication([])  # noqa: F841 - QPixmap needs a QGuiApplication
    icons_dir = Path(__file__).resolve().parent / "icons"
    icons_dir.mkdir(exist_ok=True)
    target = icons_dir / "fakes3.ico"
    pixmap = make_icon().pixmap(256, 256)
    if not pixmap.save(str(target), "ICO"):
        raise SystemExit("failed to write fakes3.ico")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
