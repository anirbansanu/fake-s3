"""Headless server entry point: `python -m fakes3`.

Reproduces the behavior of the original single-file fakes3.py — configuration
comes from FAKE_S3_* environment variables (over the persisted config file) —
plus the admin API (/_fakes3/*) used by the CLI and GUI.
"""

import uvicorn

from .config import load_server_config
from .core.engine import StorageEngine
from .logsys import RingBufferHandler, setup_logging
from .server.app import create_app
from .stats import Stats


def main() -> None:
    config = load_server_config()
    ring = RingBufferHandler()
    setup_logging(ring=ring)
    engine = StorageEngine(config)
    app = create_app(engine, stats=Stats(), ring=ring)
    uvicorn.run(app, host=config.host, port=config.port,
                log_config=None, access_log=False)


if __name__ == "__main__":
    main()
