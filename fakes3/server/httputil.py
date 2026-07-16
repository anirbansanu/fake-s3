"""HTTP-protocol helpers: Range, conditionals, presigned expiry, vhost, headers."""

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from fastapi import Request, Response
from starlette.datastructures import QueryParams

from ..core.errors import PRECONDITION_MSG, S3Error
from ..core.keys import BUCKET_RE, IP_RE, normalize_key
from .xmlgen import http_date


def parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse 'bytes=a-b' / 'bytes=a-' / 'bytes=-n' into inclusive (start, end)."""
    try:
        unit, _, spec = range_header.partition("=")
        if unit.strip().lower() != "bytes" or "," in spec:
            raise ValueError
        start_text, _, end_text = spec.strip().partition("-")
        if start_text == "":
            length = int(end_text)
            if length <= 0:
                raise ValueError
            start, end = max(file_size - length, 0), file_size - 1
        else:
            start = int(start_text)
            end = min(int(end_text), file_size - 1) if end_text else file_size - 1
        if start < 0 or start > end or start >= file_size:
            raise ValueError
    except ValueError:
        raise S3Error(416, "InvalidRange", "The requested range is not satisfiable.")
    return start, end


def conditional_response(request: Request, etag: str, mtime: float) -> Response | None:
    """If-Match / If-Unmodified-Since / If-None-Match / If-Modified-Since."""
    h = request.headers

    def tags(value: str) -> set[str]:
        return {t.strip().strip('"') for t in value.split(",")}

    if_match = h.get("if-match")
    if if_match and "*" not in tags(if_match) and etag not in tags(if_match):
        raise S3Error(412, "PreconditionFailed", PRECONDITION_MSG)

    if_unmodified = h.get("if-unmodified-since")
    if if_unmodified and not if_match:
        try:
            if mtime > parsedate_to_datetime(if_unmodified).timestamp():
                raise S3Error(412, "PreconditionFailed", PRECONDITION_MSG)
        except (TypeError, ValueError):
            pass

    not_modified = {"ETag": f'"{etag}"', "Last-Modified": http_date(mtime)}
    if_none_match = h.get("if-none-match")
    if if_none_match:
        if "*" in tags(if_none_match) or etag in tags(if_none_match):
            return Response(status_code=304, headers=not_modified)
        return None

    if_modified = h.get("if-modified-since")
    if if_modified:
        try:
            if int(mtime) <= parsedate_to_datetime(if_modified).timestamp():
                return Response(status_code=304, headers=not_modified)
        except (TypeError, ValueError):
            pass
    return None


def check_presigned_expiry(q: QueryParams) -> None:
    """
    Presigned URLs: signatures are accepted without verification (local dev),
    but expiry is honored so temporary URLs actually expire. Both presigned
    formats are recognized — SigV4 (X-Amz-Date + X-Amz-Expires) and legacy
    SigV2 (Expires = absolute unix timestamp).
    """
    limit: float | None = None
    if "X-Amz-Signature" in q and q.get("X-Amz-Date") and q.get("X-Amz-Expires"):
        try:
            t0 = datetime.strptime(q.get("X-Amz-Date"), "%Y%m%dT%H%M%SZ") \
                .replace(tzinfo=timezone.utc).timestamp()
            limit = t0 + int(q.get("X-Amz-Expires"))
        except ValueError:
            return
    elif "Signature" in q and q.get("Expires"):
        try:
            limit = float(q.get("Expires"))
        except ValueError:
            return
    if limit is not None and time.time() > limit:
        raise S3Error(403, "AccessDenied", "Request has expired")


def vhost_bucket(request: Request, vhost_bases: list[str]) -> str | None:
    """Extract the bucket from the Host header for virtual-hosted-style requests."""
    host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    if not host or IP_RE.fullmatch(host):
        return None
    if host in vhost_bases:
        return None
    for base in vhost_bases:
        if host.endswith("." + base):
            candidate = host[: -(len(base) + 1)]
            if BUCKET_RE.fullmatch(candidate):
                return candidate
    return None


def user_metadata(request: Request) -> dict:
    return {
        name[len("x-amz-meta-"):]: value
        for name, value in request.headers.items()
        if name.startswith("x-amz-meta-")
    }


def parse_copy_source(request: Request) -> tuple[str, str]:
    source = unquote(request.headers.get("x-amz-copy-source", "")).split("?", 1)[0]
    bucket, _, raw_key = source.lstrip("/").partition("/")
    if not bucket or not raw_key:
        raise S3Error(400, "InvalidArgument", "Invalid x-amz-copy-source header.")
    key, is_marker = normalize_key(raw_key)
    if is_marker:
        raise S3Error(400, "InvalidArgument", "Cannot copy a directory marker.")
    return bucket, key
