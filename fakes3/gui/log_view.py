"""Live log view fed by the ring buffer (access log, errors, server events)."""

import logging
from datetime import datetime

from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from .state import AppState

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class LogView(QWidget):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state

        self.level_combo = QComboBox()
        self.level_combo.addItems(LEVELS)
        self.level_combo.setCurrentText("INFO")
        self.level_combo.currentTextChanged.connect(self.reload)
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        self.pause = QCheckBox("Pause")
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.clear)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Level:"))
        controls.addWidget(self.level_combo)
        controls.addWidget(self.autoscroll)
        controls.addWidget(self.pause)
        controls.addStretch()
        controls.addWidget(clear_button)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(5000)
        self.text.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.text)

        self.state.log_record.connect(self.on_record)
        self.reload()

    def _threshold(self) -> int:
        return logging.getLevelName(self.level_combo.currentText())

    def _passes(self, record: dict) -> bool:
        level = logging.getLevelName(record["level"])
        return isinstance(level, int) and level >= self._threshold()

    @staticmethod
    def _format(record: dict) -> str:
        ts = datetime.fromtimestamp(record["ts"]).strftime("%H:%M:%S")
        return f"{ts} {record['level']:<8} {record['logger']}: {record['message']}"

    def on_record(self, record: dict) -> None:
        if self.pause.isChecked() or not self._passes(record):
            return
        self.text.appendPlainText(self._format(record))
        if self.autoscroll.isChecked():
            self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def reload(self) -> None:
        self.text.clear()
        for record in self.state.ring.records(limit=1000):
            if self._passes(record):
                self.text.appendPlainText(self._format(record))

    def clear(self) -> None:
        self.state.ring.clear()
        self.text.clear()
