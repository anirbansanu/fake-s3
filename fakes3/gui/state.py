"""
Shared GUI application state.

Owns the ServerController, the log ring buffer, and the persisted
configuration; bridges backend callbacks (which fire on server threads)
into Qt signals (delivered on the GUI thread via queued connections).
"""

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..config import ServerConfig, load_file_config, save_file_config
from ..core.engine import StorageEngine
from ..logsys import RingBufferHandler, setup_logging
from ..server.controller import ServerController

APP_PREF_DEFAULTS = {
    "minimize_to_tray": True,
    "notifications": True,
    "start_with_windows": False,
    "start_server_on_launch": True,
    "log_file": "",
}


class AppState(QObject):
    log_record = Signal(dict)        # every log line (from any thread)
    server_event = Signal(str, str)  # started / stopped / restarted / error


    def __init__(self):
        super().__init__()
        data = load_file_config()
        self.app_prefs = {**APP_PREF_DEFAULTS, **data.get("app", {})}

        server_config = ServerConfig.from_dict(data.get("server", {}))
        self.ring = RingBufferHandler()
        log_file = self.app_prefs.get("log_file") or None
        setup_logging(ring=self.ring, console=False,
                      log_file=Path(log_file) if log_file else None)
        self.controller = ServerController(
            server_config, ring=self.ring,
            on_event=lambda event, message: self.server_event.emit(event, message))
        self.ring.subscribe(lambda record: self.log_record.emit(record))

    # -- engine access -----------------------------------------------------------

    def current_engine(self) -> StorageEngine:
        """The running server's engine, or a standalone one over the same config."""
        if self.controller.is_running and self.controller.engine is not None:
            return self.controller.engine
        return StorageEngine(self.controller.config)

    # -- persistence ----------------------------------------------------------------

    def save_config(self, server_config: ServerConfig | None = None,
                    app_prefs: dict | None = None) -> None:
        data = load_file_config()
        if server_config is not None:
            data["server"] = server_config.to_dict()
            self.controller.config = server_config
        if app_prefs is not None:
            self.app_prefs.update(app_prefs)
            data["app"] = self.app_prefs
        save_file_config(data)
