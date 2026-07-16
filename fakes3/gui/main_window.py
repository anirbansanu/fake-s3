"""Main window: navigation sidebar + stacked pages + tray integration."""

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListWidget,
                               QMainWindow, QMenu, QStackedWidget, QSystemTrayIcon,
                               QWidget)

from .browser import BrowserPage
from .log_view import LogView
from .server_panel import ServerPanel
from .settings_page import SettingsPage
from .state import AppState
from .stats_page import StatsPage

PAGES = ["Server", "Browser", "Logs", "Statistics", "Settings"]


class MainWindow(QMainWindow):
    def __init__(self, state: AppState, icon: QIcon):
        super().__init__()
        self.state = state
        self.icon = icon
        self.quitting = False
        self.tray_hint_shown = False

        self.setWindowTitle("fakes3 — local S3 server")
        self.setWindowIcon(icon)
        self.resize(900, 620)

        self.nav = QListWidget()
        self.nav.addItems(PAGES)
        self.nav.setFixedWidth(140)
        self.nav.setCurrentRow(0)

        self.pages = QStackedWidget()
        self.pages.addWidget(ServerPanel(state))
        self.pages.addWidget(BrowserPage(state))
        self.pages.addWidget(LogView(state))
        self.pages.addWidget(StatsPage(state))
        self.pages.addWidget(SettingsPage(state))
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(self.nav)
        layout.addWidget(self.pages, stretch=1)
        self.setCentralWidget(central)

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label)
        status_timer = QTimer(self)
        status_timer.timeout.connect(self.update_status_bar)
        status_timer.start(1000)
        self.update_status_bar()

        self.tray = self.build_tray()
        self.state.server_event.connect(self.on_server_event)

    # -- tray ------------------------------------------------------------------

    def build_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(self.icon, self)
        tray.setToolTip("fakes3 — local S3 server")
        menu = QMenu()

        show_action = QAction("Open fakes3", menu)
        show_action.triggered.connect(self.show_from_tray)
        start_action = QAction("Start server", menu)
        start_action.triggered.connect(self.tray_start)
        stop_action = QAction("Stop server", menu)
        stop_action.triggered.connect(lambda: self.state.controller.stop())
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit_app)

        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(start_action)
        menu.addAction(stop_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self.show_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        return tray

    def tray_start(self) -> None:
        try:
            self.state.controller.start()
        except RuntimeError:
            pass  # the error event is already notified + logged

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # -- notifications ----------------------------------------------------------

    def on_server_event(self, event: str, message: str) -> None:
        self.update_status_bar()
        if self.tray is None or not self.state.app_prefs.get("notifications", True):
            return
        icons = {
            "started": QSystemTrayIcon.MessageIcon.Information,
            "restarted": QSystemTrayIcon.MessageIcon.Information,
            "stopped": QSystemTrayIcon.MessageIcon.Warning,
            "error": QSystemTrayIcon.MessageIcon.Critical,
        }
        self.tray.showMessage("fakes3", message,
                              icons.get(event, QSystemTrayIcon.MessageIcon.Information),
                              4000)

    def update_status_bar(self) -> None:
        controller = self.state.controller
        if controller.is_running:
            uptime = int(controller.uptime_seconds)
            self.status_label.setText(
                f"Running on {controller.endpoint} — uptime "
                f"{uptime // 3600}h {uptime % 3600 // 60}m {uptime % 60}s")
        else:
            self.status_label.setText("Server stopped")

    # -- lifecycle -----------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if (not self.quitting and self.tray is not None
                and self.state.app_prefs.get("minimize_to_tray", True)):
            event.ignore()
            self.hide()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                if self.state.app_prefs.get("notifications", True):
                    self.tray.showMessage(
                        "fakes3", "Still running in the system tray. "
                        "Right-click the icon to quit.",
                        QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        event.accept()

    def quit_app(self) -> None:
        self.quitting = True
        self.state.controller.stop()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()
