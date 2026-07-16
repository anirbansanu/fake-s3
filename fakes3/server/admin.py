"""
Admin API — the control surface used by the CLI and external tools.

Lives under /_fakes3/. Bucket names can never contain "_" (see BUCKET_RE),
so this prefix cannot collide with any S3 request. Like the S3 API itself,
it is unauthenticated: fakes3 is a local development tool.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .. import __version__
from ..core.engine import StorageEngine
from ..logsys import RingBufferHandler
from ..stats import Stats

ADMIN_PREFIX = "/_fakes3"


def register_admin(app: FastAPI, engine: StorageEngine,
                   stats: Stats, ring: RingBufferHandler) -> None:
    """Register admin routes. Must be called BEFORE the catch-all S3 route."""

    @app.get(f"{ADMIN_PREFIX}/status")
    async def admin_status() -> JSONResponse:
        return JSONResponse({
            "status": "running",
            "version": __version__,
            "uptime_seconds": stats.snapshot()["uptime_seconds"],
            "config": engine.config.to_dict(),
            "storage_root": str(engine.storage_root),
        })

    @app.get(f"{ADMIN_PREFIX}/stats")
    async def admin_stats() -> JSONResponse:
        data = stats.snapshot()
        data["storage"] = engine.storage_usage()
        return JSONResponse(data)

    @app.get(f"{ADMIN_PREFIX}/logs")
    async def admin_logs(limit: int = 200, level: str | None = None) -> JSONResponse:
        return JSONResponse({"logs": ring.records(limit=max(1, min(limit, 2000)),
                                                  min_level=level)})

    @app.get(f"{ADMIN_PREFIX}/config")
    async def admin_config() -> JSONResponse:
        return JSONResponse(engine.config.to_dict())
