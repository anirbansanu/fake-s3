"""Settings: server configuration + application preferences + import/export."""

import json
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ..config import ServerConfig, load_file_config, save_file_config
from . import autostart
from .state import AppState


class SettingsPage(QWidget):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state

        # -- server settings ---------------------------------------------------
        config = state.controller.config
        self.host_edit = QLineEdit(config.host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(config.port)
        self.storage_edit = QLineEdit(str(config.storage_root))
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self.browse_storage)
        storage_row = QHBoxLayout()
        storage_row.addWidget(self.storage_edit)
        storage_row.addWidget(browse_button)
        self.region_edit = QLineEdit(config.region)
        self.single_bucket_check = QCheckBox(
            "Single-bucket mode (every bucket name aliases one shared root)")
        self.single_bucket_check.setChecked(config.single_bucket)
        self.bucket_name_edit = QLineEdit(config.bucket_name)
        self.auto_create_check = QCheckBox("Auto-create buckets on first write")
        self.auto_create_check.setChecked(config.auto_create)
        self.vhost_edit = QLineEdit(",".join(config.vhost_bases))

        server_box = QGroupBox("Server")
        server_form = QFormLayout(server_box)
        server_form.addRow("Host:", self.host_edit)
        server_form.addRow("Port:", self.port_spin)
        server_form.addRow("Storage folder:", storage_row)
        server_form.addRow("Region:", self.region_edit)
        server_form.addRow("", self.single_bucket_check)
        server_form.addRow("Bucket name:", self.bucket_name_edit)
        server_form.addRow("", self.auto_create_check)
        server_form.addRow("V-host bases:", self.vhost_edit)

        # -- application preferences ------------------------------------------------
        prefs = state.app_prefs
        self.tray_check = QCheckBox("Minimize to system tray on close")
        self.tray_check.setChecked(prefs["minimize_to_tray"])
        self.notify_check = QCheckBox("Show notifications (start/stop/errors)")
        self.notify_check.setChecked(prefs["notifications"])
        self.windows_check = QCheckBox("Start automatically with Windows")
        self.windows_check.setChecked(autostart.is_enabled())
        self.launch_check = QCheckBox("Start server when the app opens")
        self.launch_check.setChecked(prefs["start_server_on_launch"])
        self.log_file_edit = QLineEdit(prefs.get("log_file") or "")
        log_browse = QPushButton("Browse…")
        log_browse.clicked.connect(self.browse_log_file)
        log_row = QHBoxLayout()
        log_row.addWidget(self.log_file_edit)
        log_row.addWidget(log_browse)

        app_box = QGroupBox("Application")
        app_form = QFormLayout(app_box)
        app_form.addRow("", self.tray_check)
        app_form.addRow("", self.notify_check)
        app_form.addRow("", self.windows_check)
        app_form.addRow("", self.launch_check)
        app_form.addRow("Log file (optional):", log_row)

        # -- buttons -------------------------------------------------------------------
        save_button = QPushButton("Save settings")
        save_button.clicked.connect(self.save)
        import_button = QPushButton("Import…")
        import_button.clicked.connect(self.import_config)
        export_button = QPushButton("Export…")
        export_button.clicked.connect(self.export_config)
        buttons = QHBoxLayout()
        buttons.addWidget(save_button)
        buttons.addStretch()
        buttons.addWidget(import_button)
        buttons.addWidget(export_button)

        layout = QVBoxLayout(self)
        layout.addWidget(server_box)
        layout.addWidget(app_box)
        layout.addLayout(buttons)
        layout.addStretch()

    # -- helpers ------------------------------------------------------------------

    def browse_storage(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose storage folder",
                                                  self.storage_edit.text())
        if chosen:
            self.storage_edit.setText(chosen)

    def browse_log_file(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Choose log file",
                                                self.log_file_edit.text() or "fakes3.log",
                                                "Log files (*.log);;All files (*)")
        if chosen:
            self.log_file_edit.setText(chosen)

    def current_server_config(self) -> ServerConfig:
        return ServerConfig(
            storage_root=Path(self.storage_edit.text().strip() or "storage"),
            host=self.host_edit.text().strip() or "0.0.0.0",
            port=self.port_spin.value(),
            region=self.region_edit.text().strip() or "us-east-1",
            single_bucket=self.single_bucket_check.isChecked(),
            bucket_name=self.bucket_name_edit.text().strip() or "mybucket",
            auto_create=self.auto_create_check.isChecked(),
            vhost_bases=[h.strip() for h in self.vhost_edit.text().split(",") if h.strip()],
        )

    def save(self) -> None:
        try:
            server_config = self.current_server_config()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "fakes3", f"Invalid settings: {exc}")
            return

        prefs = {
            "minimize_to_tray": self.tray_check.isChecked(),
            "notifications": self.notify_check.isChecked(),
            "start_with_windows": self.windows_check.isChecked(),
            "start_server_on_launch": self.launch_check.isChecked(),
            "log_file": self.log_file_edit.text().strip(),
        }
        self.state.save_config(server_config=server_config, app_prefs=prefs)
        try:
            autostart.set_enabled(self.windows_check.isChecked())
        except OSError as exc:
            QMessageBox.warning(self, "fakes3", f"Could not update Windows autostart: {exc}")

        if self.state.controller.is_running:
            answer = QMessageBox.question(
                self, "fakes3",
                "Settings saved. Restart the server now to apply them?")
            if answer == QMessageBox.StandardButton.Yes:
                try:
                    self.state.controller.restart(server_config)
                except RuntimeError as exc:
                    QMessageBox.critical(self, "fakes3", str(exc))
        else:
            QMessageBox.information(self, "fakes3", "Settings saved.")

    def import_config(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Import configuration", "",
                                                "JSON files (*.json);;All files (*)")
        if not chosen:
            return
        try:
            data = json.loads(Path(chosen).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration file must contain a JSON object")
            ServerConfig.from_dict(data.get("server", {}))  # validate
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "fakes3", f"Import failed: {exc}")
            return
        save_file_config(data)
        QMessageBox.information(
            self, "fakes3",
            "Configuration imported. Reopen the app (or adjust and save) to apply it.")

    def export_config(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Export configuration",
                                                "fakes3-config.json",
                                                "JSON files (*.json);;All files (*)")
        if not chosen:
            return
        try:
            Path(chosen).write_text(json.dumps(load_file_config(), indent=2),
                                    encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "fakes3", f"Export failed: {exc}")
            return
        QMessageBox.information(self, "fakes3", f"Configuration exported to {chosen}")
