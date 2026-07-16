"""Bucket & object browser page."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.errors import S3Error
from . import browser_ops
from .state import AppState


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ObjectTable(QTableWidget):
    """Object list with drag-and-drop upload support."""

    def __init__(self, on_files_dropped):
        super().__init__(0, 3)
        self._on_files_dropped = on_files_dropped
        self.setHorizontalHeaderLabels(["Key", "Size", "Modified"])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()
                 if url.isLocalFile()]
        files = [p for p in paths if p.is_file()]
        if files:
            self._on_files_dropped(files)
            event.acceptProposedAction()


class MetadataDialog(QDialog):
    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Metadata — {info['key']}")
        form = QFormLayout(self)

        def row(label: str, value: str) -> None:
            widget = QLabel(value)
            widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(label, widget)

        row("Key:", info["key"])
        row("Size:", f"{info['size']} bytes ({human_size(info['size'])})")
        row("ETag:", info["etag"] or "—")
        row("Content-Type:", info["content_type"] or "—")
        row("Modified:", datetime.fromtimestamp(info["mtime"]).strftime("%Y-%m-%d %H:%M:%S"))
        if info.get("path"):
            row("Stored at:", info["path"])
        for name, value in sorted(info.get("meta", {}).items()):
            row(f"x-amz-meta-{name}:", value)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        form.addRow("", close_button)


class BrowserPage(QWidget):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state

        # -- bucket row ---------------------------------------------------------
        self.bucket_combo = QComboBox()
        self.bucket_combo.currentTextChanged.connect(lambda *_: self.reload_objects())
        new_bucket = QPushButton("New bucket")
        new_bucket.clicked.connect(self.create_bucket)
        self.rename_bucket_button = QPushButton("Rename bucket")
        self.rename_bucket_button.clicked.connect(self.rename_bucket)
        self.delete_bucket_button = QPushButton("Delete bucket")
        self.delete_bucket_button.clicked.connect(self.delete_bucket)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload_all)

        bucket_row = QHBoxLayout()
        bucket_row.addWidget(QLabel("Bucket:"))
        bucket_row.addWidget(self.bucket_combo, stretch=1)
        bucket_row.addWidget(new_bucket)
        bucket_row.addWidget(self.rename_bucket_button)
        bucket_row.addWidget(self.delete_bucket_button)
        bucket_row.addWidget(refresh)

        # -- object actions ----------------------------------------------------------
        upload_button = QPushButton("Upload…")
        upload_button.clicked.connect(self.upload_files)
        download_button = QPushButton("Download…")
        download_button.clicked.connect(self.download_selected)
        folder_button = QPushButton("New folder")
        folder_button.clicked.connect(self.new_folder)
        rename_button = QPushButton("Rename/Move")
        rename_button.clicked.connect(self.rename_selected)
        copy_button = QPushButton("Copy")
        copy_button.clicked.connect(self.copy_selected)
        delete_button = QPushButton("Delete")
        delete_button.clicked.connect(self.delete_selected)
        meta_button = QPushButton("Metadata")
        meta_button.clicked.connect(self.show_metadata)

        actions = QHBoxLayout()
        for button in (upload_button, download_button, folder_button, rename_button,
                       copy_button, delete_button, meta_button):
            actions.addWidget(button)
        actions.addStretch()

        self.table = ObjectTable(self.upload_dropped)
        self.count_label = QLabel("")

        layout = QVBoxLayout(self)
        layout.addLayout(bucket_row)
        layout.addLayout(actions)
        layout.addWidget(self.table)
        layout.addWidget(self.count_label)

        self.state.server_event.connect(lambda *_: self.reload_all())
        self.reload_all()

    # -- helpers -------------------------------------------------------------------

    def engine(self):
        return self.state.current_engine()

    def current_bucket(self) -> str | None:
        return self.bucket_combo.currentText() or None

    def selected_key(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).text()

    def guard(self, action, *args) -> bool:
        try:
            action(*args)
            return True
        except (S3Error, OSError) as exc:
            message = exc.message if isinstance(exc, S3Error) else str(exc)
            QMessageBox.warning(self, "fakes3", message)
            return False

    # -- loading -------------------------------------------------------------------

    def reload_all(self) -> None:
        engine = self.engine()
        single = engine.config.single_bucket
        current = self.current_bucket()
        self.bucket_combo.blockSignals(True)
        self.bucket_combo.clear()
        buckets = browser_ops.list_buckets(engine)
        self.bucket_combo.addItems(buckets)
        if current in buckets:
            self.bucket_combo.setCurrentText(current)
        self.bucket_combo.blockSignals(False)
        self.rename_bucket_button.setEnabled(not single)
        self.delete_bucket_button.setEnabled(not single)
        self.reload_objects()

    def reload_objects(self) -> None:
        bucket = self.current_bucket()
        self.table.setRowCount(0)
        if not bucket:
            self.count_label.setText("no buckets")
            return
        try:
            rows = browser_ops.list_objects(self.engine(), bucket)
        except S3Error:
            rows = []
        self.table.setRowCount(len(rows))
        total = 0
        for index, row in enumerate(rows):
            total += row["size"]
            self.table.setItem(index, 0, QTableWidgetItem(row["key"]))
            size_item = QTableWidgetItem("—" if row["is_marker"] else human_size(row["size"]))
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(index, 1, size_item)
            modified = datetime.fromtimestamp(row["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            self.table.setItem(index, 2, QTableWidgetItem(modified))
        self.count_label.setText(f"{len(rows)} object(s), {human_size(total)}")

    # -- bucket actions ------------------------------------------------------------

    def create_bucket(self) -> None:
        if self.engine().config.single_bucket:
            QMessageBox.information(
                self, "fakes3",
                "Single-bucket mode: every bucket name aliases the shared storage "
                "root. Switch to multi-bucket mode in Settings to manage buckets.")
            return
        name, ok = QInputDialog.getText(self, "New bucket", "Bucket name:")
        if ok and name:
            if self.guard(browser_ops.create_bucket, self.engine(), name.strip()):
                self.reload_all()
                self.bucket_combo.setCurrentText(name.strip())

    def rename_bucket(self) -> None:
        bucket = self.current_bucket()
        if not bucket:
            return
        name, ok = QInputDialog.getText(self, "Rename bucket", "New name:", text=bucket)
        if ok and name and name != bucket:
            if self.guard(browser_ops.rename_bucket, self.engine(), bucket, name.strip()):
                self.reload_all()
                self.bucket_combo.setCurrentText(name.strip())

    def delete_bucket(self) -> None:
        bucket = self.current_bucket()
        if not bucket:
            return
        answer = QMessageBox.question(
            self, "fakes3",
            f"Delete bucket '{bucket}' and ALL objects in it?")
        if answer == QMessageBox.StandardButton.Yes:
            if self.guard(browser_ops.delete_bucket, self.engine(), bucket):
                self.reload_all()

    # -- object actions ---------------------------------------------------------------

    def upload_files(self) -> None:
        bucket = self.current_bucket()
        if not bucket:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        self.upload_dropped([Path(f) for f in files])

    def upload_dropped(self, files: list[Path]) -> None:
        bucket = self.current_bucket()
        if not bucket or not files:
            return
        prefix, ok = QInputDialog.getText(
            self, "Upload", "Key prefix (empty for bucket root):")
        if not ok:
            return
        prefix = prefix.strip().strip("/")
        for path in files:
            key = f"{prefix}/{path.name}" if prefix else path.name
            if not self.guard(browser_ops.upload, self.engine(), bucket, key, path):
                break
        self.reload_objects()

    def download_selected(self) -> None:
        bucket, key = self.current_bucket(), self.selected_key()
        if not bucket or not key:
            return
        if key.endswith("/"):
            QMessageBox.information(self, "fakes3", "Folders cannot be downloaded.")
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Download to",
                                              key.rsplit("/", 1)[-1])
        if dest:
            if self.guard(browser_ops.download, self.engine(), bucket, key, Path(dest)):
                self.count_label.setText(f"downloaded {key} -> {dest}")

    def new_folder(self) -> None:
        bucket = self.current_bucket()
        if not bucket:
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder key (e.g. exports/2026):")
        if ok and name.strip():
            if self.guard(browser_ops.new_folder, self.engine(), bucket, name.strip()):
                self.reload_objects()

    def rename_selected(self) -> None:
        bucket, key = self.current_bucket(), self.selected_key()
        if not bucket or not key:
            return
        if key.endswith("/"):
            QMessageBox.information(self, "fakes3", "Folders cannot be renamed directly.")
            return
        new_key, ok = QInputDialog.getText(self, "Rename / move object",
                                           "New key:", text=key)
        if ok and new_key and new_key != key:
            if self.guard(browser_ops.move_object, self.engine(), bucket, key, new_key.strip()):
                self.reload_objects()

    def copy_selected(self) -> None:
        bucket, key = self.current_bucket(), self.selected_key()
        if not bucket or not key:
            return
        if key.endswith("/"):
            QMessageBox.information(self, "fakes3", "Folders cannot be copied.")
            return
        new_key, ok = QInputDialog.getText(self, "Copy object",
                                           "Destination key:", text=key)
        if ok and new_key and new_key != key:
            if self.guard(browser_ops.copy_object, self.engine(), bucket, key, new_key.strip()):
                self.reload_objects()

    def delete_selected(self) -> None:
        bucket, key = self.current_bucket(), self.selected_key()
        if not bucket or not key:
            return
        answer = QMessageBox.question(self, "fakes3", f"Delete '{key}'?")
        if answer == QMessageBox.StandardButton.Yes:
            if self.guard(browser_ops.delete_object, self.engine(), bucket, key):
                self.reload_objects()

    def show_metadata(self) -> None:
        bucket, key = self.current_bucket(), self.selected_key()
        if not bucket or not key:
            return
        try:
            info = browser_ops.head_object(self.engine(), bucket, key)
        except (S3Error, OSError) as exc:
            message = exc.message if isinstance(exc, S3Error) else str(exc)
            QMessageBox.warning(self, "fakes3", message)
            return
        MetadataDialog(info, self).exec()
