"""
ServerController — run the fakes3 server inside the current process.

Wraps uvicorn.Server in a daemon thread so a host application (the GUI, or
the CLI `serve` command) can start, stop, and restart the listener without
managing child processes. Each start builds a fresh engine + app from the
current config, so config changes take effect on restart.
"""

import asyncio
import socket
import threading
import time
from typing import Callable

import uvicorn

from ..config import ServerConfig
from ..core.engine import StorageEngine
from ..logsys import RingBufferHandler, event_logger
from ..stats import Stats
from .app import create_app

EventCallback = Callable[[str, str], None]  # (event, message)


class ServerController:
    START_TIMEOUT = 15.0
    STOP_TIMEOUT = 10.0

    def __init__(self, config: ServerConfig,
                 ring: RingBufferHandler | None = None,
                 on_event: EventCallback | None = None):
        self.config = config
        self.ring = ring if ring is not None else RingBufferHandler()
        self.stats = Stats()
        self.engine: StorageEngine | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._started_at: float | None = None
        self._on_event = on_event
        self._lock = threading.Lock()

    # -- state -----------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and self._server is not None and self._server.started)

    @property
    def uptime_seconds(self) -> float:
        if not self.is_running or self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    @property
    def endpoint(self) -> str:
        host = self.config.host
        if host in ("0.0.0.0", "::"):
            host = "localhost"
        return f"http://{host}:{self.config.port}"

    @property
    def last_error(self) -> str | None:
        return str(self._error) if self._error else None

    def _emit(self, event: str, message: str) -> None:
        event_logger().info("%s: %s", event, message)
        if self._on_event:
            try:
                self._on_event(event, message)
            except Exception:  # noqa: BLE001 - listener errors must not break control flow
                pass

    # -- lifecycle ---------------------------------------------------------------

    @staticmethod
    def port_in_use(host: str, port: int) -> bool:
        """
        True if something already accepts connections on the port. Needed on
        Windows, where SO_REUSEADDR (set by uvicorn) lets a second server bind
        an occupied port silently instead of failing.
        """
        probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "localhost") else host
        try:
            with socket.create_connection((probe_host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def start(self) -> None:
        """Start the listener. Raises RuntimeError if it cannot come up."""
        with self._lock:
            if self.is_running:
                return
            if self.port_in_use(self.config.host, self.config.port):
                message = (f"Port {self.config.port} is already in use — "
                           "is another server running?")
                self._emit("error", f"Server failed to start: {message}")
                raise RuntimeError(message)
            self._error = None
            self.stats.reset()
            self.engine = StorageEngine(self.config)
            app = create_app(self.engine, stats=self.stats, ring=self.ring)
            uv_config = uvicorn.Config(
                app,
                host=self.config.host,
                port=self.config.port,
                log_config=None,     # propagate to the root logger (logsys)
                access_log=False,    # our own access middleware logs requests
            )
            self._server = uvicorn.Server(uv_config)

            def run() -> None:
                try:
                    asyncio.run(self._server.serve())
                except BaseException as exc:  # noqa: BLE001 - surface to the caller
                    self._error = exc

            self._thread = threading.Thread(target=run, name="fakes3-server", daemon=True)
            self._thread.start()

            deadline = time.time() + self.START_TIMEOUT
            while time.time() < deadline:
                if self._server.started:
                    break
                if self._error is not None or not self._thread.is_alive():
                    break
                time.sleep(0.05)

            if not self._server.started:
                error = self._error or RuntimeError("server did not start in time")
                self._server = None
                self._thread = None
                self._emit("error", f"Server failed to start: {error}")
                raise RuntimeError(f"Server failed to start: {error}")

            self._started_at = time.time()
            self._emit("started", f"Server listening on {self.endpoint}")

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            if server is None or thread is None:
                return
            server.should_exit = True
            thread.join(self.STOP_TIMEOUT)
            if thread.is_alive():  # graceful shutdown stuck — force it
                server.force_exit = True
                thread.join(self.STOP_TIMEOUT)
            self._server = None
            self._thread = None
            self._started_at = None
            self._emit("stopped", "Server stopped")

    def restart(self, new_config: ServerConfig | None = None) -> None:
        self.stop()
        if new_config is not None:
            self.config = new_config
        self.start()
        self._emit("restarted", f"Server restarted on {self.endpoint}")
