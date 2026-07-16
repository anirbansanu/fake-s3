"""Statistics dashboard: request counters, transfer volumes, storage usage."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from .state import AppState


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class StatsPage(QWidget):
    REFRESH_MS = 2000

    def __init__(self, state: AppState):
        super().__init__()
        self.state = state

        self.labels: dict[str, QLabel] = {}

        requests_box = QGroupBox("Requests (since last server start)")
        grid = QGridLayout(requests_box)
        for row, (key, title) in enumerate([
            ("requests", "Total requests:"),
            ("by_method", "By method:"),
            ("errors", "Errors:"),
            ("transfer", "Transferred:"),
        ]):
            grid.addWidget(QLabel(title), row, 0, Qt.AlignTop)
            label = QLabel("—")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setWordWrap(True)
            grid.addWidget(label, row, 1)
            self.labels[key] = label
        grid.setColumnStretch(1, 1)

        storage_box = QGroupBox("Storage")
        storage_grid = QGridLayout(storage_box)
        for row, (key, title) in enumerate([
            ("objects", "Objects:"),
            ("bytes", "Total size:"),
            ("root", "Location:"),
        ]):
            storage_grid.addWidget(QLabel(title), row, 0)
            label = QLabel("—")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            storage_grid.addWidget(label, row, 1)
            self.labels[f"storage_{key}"] = label
        storage_grid.setColumnStretch(1, 1)

        refresh_button = QPushButton("Refresh now")
        refresh_button.clicked.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.addWidget(requests_box)
        layout.addWidget(storage_box)
        layout.addWidget(refresh_button)
        layout.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(self.REFRESH_MS)
        self.refresh()

    def refresh(self) -> None:
        if not self.isVisible():
            return
        snapshot = self.state.controller.stats.snapshot()
        self.labels["requests"].setText(str(snapshot["requests"]))
        methods = snapshot["by_method"]
        self.labels["by_method"].setText(
            ", ".join(f"{m}: {n}" for m, n in sorted(methods.items())) or "—")
        self.labels["errors"].setText(
            f"{snapshot['errors_client']} client (4xx), "
            f"{snapshot['errors_server']} server (5xx)")
        self.labels["transfer"].setText(
            f"in {human_size(snapshot['bytes_in'])}, out {human_size(snapshot['bytes_out'])}")

        try:
            engine = self.state.current_engine()
            usage = engine.storage_usage()
            self.labels["storage_objects"].setText(str(usage["objects"]))
            self.labels["storage_bytes"].setText(human_size(usage["bytes"]))
            self.labels["storage_root"].setText(str(engine.storage_root))
        except OSError:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()
