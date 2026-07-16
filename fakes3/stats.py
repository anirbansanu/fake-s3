"""Request statistics, shared by the admin API, CLI, and GUI dashboard."""

import threading
import time
from collections import Counter


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.started_at = time.time()
            self.requests = 0
            self.errors_client = 0   # 4xx
            self.errors_server = 0   # 5xx
            self.bytes_in = 0
            self.bytes_out = 0
            self.by_method: Counter[str] = Counter()
            self.by_status: Counter[int] = Counter()

    def record(self, method: str, status: int, bytes_in: int, bytes_out: int) -> None:
        with self._lock:
            self.requests += 1
            self.by_method[method] += 1
            self.by_status[status] += 1
            self.bytes_in += bytes_in
            self.bytes_out += bytes_out
            if 400 <= status < 500:
                self.errors_client += 1
            elif status >= 500:
                self.errors_server += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "requests": self.requests,
                "errors_client": self.errors_client,
                "errors_server": self.errors_server,
                "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out,
                "by_method": dict(self.by_method),
                "by_status": {str(k): v for k, v in self.by_status.items()},
            }
