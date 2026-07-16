"""GUI application entry point."""

import sys

from PySide6.QtWidgets import QApplication

from .. import __version__
from .main_window import MainWindow
from .resources import make_icon
from .state import AppState


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("fakes3")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)  # tray keeps us alive

    icon = make_icon()
    app.setWindowIcon(icon)

    state = AppState()
    window = MainWindow(state, icon)
    window.show()

    if state.app_prefs.get("start_server_on_launch", True):
        try:
            state.controller.start()
        except RuntimeError:
            pass  # surfaced via the error notification + server panel

    app.aboutToQuit.connect(state.controller.stop)
    return app.exec()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
