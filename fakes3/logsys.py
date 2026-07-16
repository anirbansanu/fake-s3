"""
Logging system.

All server logging funnels through the standard `logging` tree. A
RingBufferHandler keeps the most recent records in memory for the admin API
(`/_fakes3/logs`) and the GUI log view (which subscribes for live updates).
"""

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

ACCESS_LOGGER = "fakes3.access"
EVENT_LOGGER = "fakes3.event"

LogCallback = Callable[[dict], None]


class RingBufferHandler(logging.Handler):
    """Keeps the last `capacity` log records as dicts; supports live subscribers."""

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._records: deque[dict] = deque(maxlen=capacity)
        self._lock2 = threading.Lock()
        self._subscribers: list[LogCallback] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        except Exception:  # noqa: BLE001 - a broken record must not kill the server
            return
        with self._lock2:
            self._records.append(entry)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(entry)
            except Exception:  # noqa: BLE001 - GUI callback errors stay out of the server
                pass

    def subscribe(self, callback: LogCallback) -> None:
        with self._lock2:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: LogCallback) -> None:
        with self._lock2:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def records(self, limit: int = 200, min_level: str | None = None) -> list[dict]:
        with self._lock2:
            items = list(self._records)
        if min_level:
            threshold = logging.getLevelName(min_level.upper())
            if isinstance(threshold, int):
                items = [
                    r for r in items
                    if isinstance(logging.getLevelName(r["level"]), int)
                    and logging.getLevelName(r["level"]) >= threshold
                ]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock2:
            self._records.clear()


def setup_logging(ring: RingBufferHandler | None = None,
                  log_file: Path | None = None,
                  console: bool = True,
                  level: int = logging.INFO) -> None:
    """
    Configure the root logger. uvicorn is run with log_config=None so its
    loggers propagate here — one pipeline for console, file, and ring buffer.
    """
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    def has_handler(kind: type) -> bool:
        return any(isinstance(h, kind) for h in root.handlers)

    if console and not has_handler(logging.StreamHandler):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)
    if ring is not None and ring not in root.handlers:
        root.addHandler(ring)
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def access_logger() -> logging.Logger:
    return logging.getLogger(ACCESS_LOGGER)


def event_logger() -> logging.Logger:
    return logging.getLogger(EVENT_LOGGER)


def format_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
