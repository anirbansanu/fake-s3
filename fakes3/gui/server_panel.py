"""Server control panel: status, uptime, start/stop/restart."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .state import AppState


class ServerPanel(QWidget):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state

        self.status_label = QLabel()
        self.status_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.endpoint_label = QLabel()
        self.endpoint_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.uptime_label = QLabel("—")
        self.storage_label = QLabel()
        self.storage_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mode_label = QLabel()

        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        self.start_button.clicked.connect(self.start_server)
        self.stop_button.clicked.connect(self.stop_server)
        self.restart_button.clicked.connect(self.restart_server)

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.restart_button)
        buttons.addStretch()

        info_box = QGroupBox("Server")
        grid = QGridLayout(info_box)
        for row, (label, widget) in enumerate([
            ("Status:", self.status_label),
            ("Endpoint:", self.endpoint_label),
            ("Uptime:", self.uptime_label),
            ("Storage:", self.storage_label),
            ("Mode:", self.mode_label),
        ]):
            grid.addWidget(QLabel(label), row, 0, Qt.AlignTop)
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(info_box)
        layout.addLayout(buttons)
        layout.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.state.server_event.connect(lambda *_: self.refresh())
        self.refresh()

    # -- actions -------------------------------------------------------------

    def start_server(self) -> None:
        try:
            self.state.controller.start()
        except RuntimeError as exc:
            QMessageBox.critical(self, "fakes3", str(exc))
        self.refresh()

    def stop_server(self) -> None:
        self.state.controller.stop()
        self.refresh()

    def restart_server(self) -> None:
        try:
            self.state.controller.restart()
        except RuntimeError as exc:
            QMessageBox.critical(self, "fakes3", str(exc))
        self.refresh()

    # -- display ----------------------------------------------------------------

    def refresh(self) -> None:
        controller = self.state.controller
        running = controller.is_running
        if running:
            self.status_label.setText("● Running")
            self.status_label.setStyleSheet(
                "font-size: 18px; font-weight: bold; color: #2f9e6e;")
            uptime = int(controller.uptime_seconds)
            self.uptime_label.setText(
                f"{uptime // 3600}h {uptime % 3600 // 60}m {uptime % 60}s")
        else:
            self.status_label.setText("● Stopped")
            self.status_label.setStyleSheet(
                "font-size: 18px; font-weight: bold; color: #c0392b;")
            self.uptime_label.setText("—")
        config = controller.config
        self.endpoint_label.setText(controller.endpoint)
        self.storage_label.setText(str(config.storage_root))
        mode = "single-bucket" if config.single_bucket else "multi-bucket"
        self.mode_label.setText(
            f"{mode} (bucket: {config.bucket_name}, region: {config.region})")
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.restart_button.setEnabled(running)
