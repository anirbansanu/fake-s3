"""FastAPI application factory."""

import base64
import time
import uuid
from xml.sax.saxutils import escape as x

from fastapi import FastAPI, Request, Response

from ..core.engine import StorageEngine
from ..core.errors import S3Error
from ..logsys import RingBufferHandler, access_logger
from ..stats import Stats
from . import handlers
from .admin import register_admin


def create_app(engine: StorageEngine,
               stats: Stats | None = None,
               ring: RingBufferHandler | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.stats = stats
    app.state.ring = ring

    @app.exception_handler(S3Error)
    async def s3_error_handler(request: Request, exc: S3Error) -> Response:
        # Drain any unread request body first: rejecting a PUT/POST without
        # consuming its payload leaves stray bytes on the keep-alive connection,
        # which would corrupt the next request the client sends on it.
        try:
            async for _ in request.stream():
                pass
        except Exception:
            pass
        if request.method == "HEAD":  # HEAD errors carry no body, like real S3
            return Response(status_code=exc.status)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Error><Code>{x(exc.code)}</Code><Message>{x(exc.message)}</Message>"
            f"<Resource>{x(request.url.path)}</Resource>"
            f"<RequestId>{uuid.uuid4().hex[:16].upper()}</RequestId></Error>"
        )
        return Response(body, status_code=exc.status, media_type="application/xml")

    @app.middleware("http")
    async def add_amz_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("x-amz-request-id", uuid.uuid4().hex[:16].upper())
        response.headers.setdefault("x-amz-id-2", base64.b64encode(uuid.uuid4().bytes).decode())
        return response

    @app.middleware("http")
    async def access_and_stats(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        bytes_in = int(request.headers.get("content-length") or 0)
        bytes_out = int(response.headers.get("content-length") or 0)
        if stats is not None:
            stats.record(request.method, response.status_code, bytes_in, bytes_out)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        access_logger().info("%s %s -> %d (%.1f ms, in=%d, out=%d)",
                             request.method, target, response.status_code,
                             duration_ms, bytes_in, bytes_out)
        return response

    if stats is not None and ring is not None:
        register_admin(app, engine, stats, ring)

    @app.api_route("/{rest:path}", methods=["GET", "HEAD", "PUT", "POST", "DELETE"])
    async def entry(request: Request, rest: str) -> Response:
        return await handlers.dispatch(engine, request, rest)

    return app
