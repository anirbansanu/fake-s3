"""
fakes3.py — A single-file Amazon S3 server replica for local development.

Speaks the real S3 REST/XML wire protocol, so AWS SDKs work against it
directly as a drop-in replacement: boto3, the AWS SDK for PHP (and therefore
Laravel's `s3` filesystem disk / league/flysystem-aws-s3-v3), the AWS CLI, etc.

Objects are stored as plain files. By default the server runs in
single-bucket mode: every bucket name clients send is an alias for the one
shared storage root, so objects land at  ./storage/<key>
(e.g. ./storage/exports/report.xlsx). Set FAKE_S3_SINGLE_BUCKET=0 for the
real-S3 multi-bucket layout  ./storage/<bucket>/<key>.

Supported operations
    Service   ListBuckets                       GET /
    Bucket    CreateBucket                      PUT /{bucket}
              DeleteBucket                      DELETE /{bucket}
              HeadBucket                        HEAD /{bucket}
              ListObjects (v1)                  GET /{bucket}
              ListObjectsV2                     GET /{bucket}?list-type=2
              GetBucketLocation                 GET /{bucket}?location
              DeleteObjects (bulk)              POST /{bucket}?delete
              Get/PutBucketAcl (stub)           GET|PUT /{bucket}?acl
    Object    PutObject                         PUT /{bucket}/{key}
              CopyObject                        PUT /{bucket}/{key} + x-amz-copy-source
              GetObject (Range, conditionals,   GET /{bucket}/{key}
                presigned URLs, response-* overrides)
              HeadObject                        HEAD /{bucket}/{key}
              DeleteObject                      DELETE /{bucket}/{key}
              Get/PutObjectAcl (stub)           GET|PUT /{bucket}/{key}?acl
              Get/PutObjectTagging (stub)       GET|PUT /{bucket}/{key}?tagging
    Multipart CreateMultipartUpload             POST /{bucket}/{key}?uploads
              UploadPart                        PUT /{bucket}/{key}?partNumber&uploadId
              CompleteMultipartUpload           POST /{bucket}/{key}?uploadId
              AbortMultipartUpload              DELETE /{bucket}/{key}?uploadId
              ListParts                         GET /{bucket}/{key}?uploadId
              ListMultipartUploads              GET /{bucket}?uploads

Protocol behaviors implemented
    - Path-style (http://localhost:9000/bucket/key) and virtual-hosted-style
      (http://bucket.localhost:9000/key) addressing.
    - `aws-chunked` request bodies (STREAMING-AWS4-HMAC-SHA256-PAYLOAD and
      STREAMING-UNSIGNED-PAYLOAD-TRAILER framing) are decoded transparently.
    - S3 XML error documents with real error codes (NoSuchKey, NoSuchBucket,
      BucketNotEmpty, InvalidPart, PreconditionFailed, ...).
    - ETags are content MD5s (multipart: md5-of-part-md5s + "-N"), quoted,
      returned in headers and listings, cached in hidden sidecar files.
    - Content-Type and x-amz-meta-* user metadata are persisted and echoed.
    - Conditional requests (If-Match / If-None-Match / If-(Un)Modified-Since).
    - HTTP Range downloads (bytes=a-b, a-, -suffix) with 206/416 semantics.
    - Presigned URLs: signatures are NOT verified (any credentials work), but
      X-Amz-Date + X-Amz-Expires are honored and expired links get 403.
    - Empty directories appear as zero-byte "prefix/" marker objects, so
      Laravel's makeDirectory/directoryExists behave sensibly.

Not implemented: versioning, ACL enforcement (stubs return public-read),
bucket policies/CORS/lifecycle (GETs return the canonical "not found" codes,
PUTs are accepted and ignored), SigV4 signature verification, POST form
uploads, UploadPartCopy, GetObjectAttributes.

Requirements (Python 3.10+):  pip install fastapi uvicorn
Run:                          python fakes3.py

Environment overrides
    FAKE_S3_STORAGE        storage root folder (default: ./storage)
    FAKE_S3_PORT           listen port (default: 9000)
    FAKE_S3_REGION         region reported to clients (default: us-east-1)
    FAKE_S3_SINGLE_BUCKET  "1" (default) maps every bucket name onto the one
                           shared storage root (objects at storage/<key>);
                           "0" gives each bucket its own folder, like real S3.
                           Two buckets sharing one namespace is the trade-off:
                           use "0" if your app relies on distinct buckets.
    FAKE_S3_BUCKET_NAME    bucket name reported by ListBuckets in
                           single-bucket mode (default: mybucket)
    FAKE_S3_AUTO_CREATE    "1" (default) auto-creates buckets on first write;
                           "0" requires CreateBucket first, like real S3
    FAKE_S3_VHOST_BASES    comma-separated base hosts for virtual-hosted-style
                           addressing (default: localhost,host.docker.internal)

Client configuration examples
    boto3:
        boto3.client("s3", endpoint_url="http://localhost:9000",
                     aws_access_key_id="local", aws_secret_access_key="local",
                     region_name="us-east-1")
    Laravel (.env):
        FILESYSTEM_DISK=s3
        AWS_ACCESS_KEY_ID=local
        AWS_SECRET_ACCESS_KEY=local
        AWS_DEFAULT_REGION=us-east-1
        AWS_BUCKET=mybucket
        AWS_ENDPOINT=http://localhost:9000
        AWS_USE_PATH_STYLE_ENDPOINT=true

Note: GET /health is reserved as a liveness probe (a bucket literally named
"health" cannot be listed via GET). This is a local dev tool with no
authentication — do not expose it beyond your machine/network.
"""

import base64
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import AsyncIterator, Iterator
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as x

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import QueryParams

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STORAGE_ROOT = Path(os.environ.get("FAKE_S3_STORAGE", "storage")).resolve()
PORT = int(os.environ.get("FAKE_S3_PORT", "9000"))
REGION = os.environ.get("FAKE_S3_REGION", "us-east-1")
AUTO_CREATE_BUCKETS = os.environ.get("FAKE_S3_AUTO_CREATE", "1").lower() not in {"0", "false", "no"}
# Single-bucket mode (default): every bucket name is an alias for the storage
# root, so objects live directly at storage/<key> (e.g. storage/exports/x.xlsx)
# no matter which bucket name clients are configured with. Set
# FAKE_S3_SINGLE_BUCKET=0 for real-S3 multi-bucket layout (storage/<bucket>/<key>).
SINGLE_BUCKET = os.environ.get("FAKE_S3_SINGLE_BUCKET", "1").lower() not in {"0", "false", "no"}
BUCKET_DISPLAY_NAME = os.environ.get("FAKE_S3_BUCKET_NAME", "mybucket")
VHOST_BASES = [
    h.strip().lower()
    for h in os.environ.get("FAKE_S3_VHOST_BASES", "localhost,host.docker.internal").split(",")
    if h.strip()
]

CHUNK_SIZE = 1024 * 1024  # 1 MB streaming chunk size
XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # md5 of zero bytes
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Hidden bookkeeping files, never exposed as objects.
TMP_SUFFIX = ".fake-s3-part"          # in-progress atomic writes
META_SUFFIX = ".fake-s3-meta"         # per-object sidecar (etag, content-type, metadata)
RESERVED_SUFFIXES = (TMP_SUFFIX, META_SUFFIX)
MULTIPART_ROOT = STORAGE_ROOT / ".fake-s3-multipart"

PRECONDITION_MSG = "At least one of the pre-conditions you specified did not hold."

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Errors — S3 XML error documents
# ---------------------------------------------------------------------------

class S3Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"{code}: {message}")


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


# ---------------------------------------------------------------------------
# Small helpers: time formats, XML responses, name validation
# ---------------------------------------------------------------------------

def s3_time(ts: float) -> str:
    """S3 listing timestamp: 2026-07-16T12:00:00.000Z"""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def http_date(ts: float) -> str:
    """RFC 7231 date for Last-Modified headers."""
    return formatdate(ts, usegmt=True)


def xml_response(body: str, status: int = 200, headers: dict | None = None) -> Response:
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + body,
        status_code=status, media_type="application/xml", headers=headers,
    )


def local_name(tag: str) -> str:
    """Element tag without its XML namespace ('{ns}Key' -> 'Key')."""
    return tag.rsplit("}", 1)[-1]


def parse_xml(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise S3Error(400, "MalformedXML", "The XML you provided was not well-formed.")


def bucket_dir(bucket: str) -> Path:
    return STORAGE_ROOT if SINGLE_BUCKET else STORAGE_ROOT / bucket


def require_bucket(bucket: str) -> Path:
    d = bucket_dir(bucket)
    if not d.is_dir():
        raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist.")
    return d


def ensure_bucket(bucket: str) -> Path:
    """Bucket dir for write operations, honoring the auto-create setting."""
    d = bucket_dir(bucket)
    if not d.is_dir():
        if not AUTO_CREATE_BUCKETS:
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist.")
        d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_key(raw: str) -> tuple[str, bool]:
    """
    Canonicalize an object key for filesystem storage. Returns (key, is_marker)
    where is_marker means the key ended with '/' (an S3 directory marker).
    Real S3 keys are opaque strings; this store must reject traversal and
    collapse '.'/'..' since keys map to real paths.
    """
    key = raw.replace("\\", "/").lstrip("/")
    is_marker = key.endswith("/")
    key = key.rstrip("/")
    if not key:
        raise S3Error(400, "InvalidArgument", "Object key must not be empty.")
    key = posixpath.normpath(key)
    if key in {".", ""} or key.startswith(".."):
        raise S3Error(400, "InvalidArgument", "Invalid object key.")
    if key.endswith(RESERVED_SUFFIXES):
        raise S3Error(400, "InvalidArgument",
                      "Object keys ending in fake-s3 reserved suffixes are not allowed.")
    if key.split("/", 1)[0].startswith(".fake-s3"):
        raise S3Error(400, "InvalidArgument",
                      "Object keys may not target fake-s3 bookkeeping folders.")
    return key, is_marker


def resolve_safe_path(bucket: str, key: str) -> Path:
    """Absolute path for <bucket>/<key>, rejecting escapes from STORAGE_ROOT."""
    target = (bucket_dir(bucket) / key).resolve()
    if not target.is_relative_to(STORAGE_ROOT):
        raise S3Error(400, "InvalidArgument", "Invalid bucket or object key.")
    return target


def guess_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def vhost_bucket(request: Request) -> str | None:
    """Extract the bucket from the Host header for virtual-hosted-style requests."""
    host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    if not host or IP_RE.fullmatch(host):
        return None
    if host in VHOST_BASES:
        return None
    for base in VHOST_BASES:
        if host.endswith("." + base):
            candidate = host[: -(len(base) + 1)]
            if BUCKET_RE.fullmatch(candidate):
                return candidate
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


# ---------------------------------------------------------------------------
# Object metadata sidecars (etag cache, content-type, x-amz-meta-*)
# ---------------------------------------------------------------------------

def meta_file(target: Path) -> Path:
    return target.with_name(target.name + META_SUFFIX)


def hash_md5(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            md5.update(chunk)
    return md5.hexdigest()


def write_meta(target: Path, etag: str, content_type: str | None, user_meta: dict) -> dict:
    st = target.stat()
    data = {"etag": etag, "content_type": content_type, "meta": user_meta,
            "size": st.st_size, "mtime": st.st_mtime}
    meta_file(target).write_text(json.dumps(data), encoding="utf-8")
    return data


def object_meta(target: Path) -> dict:
    """Sidecar metadata for an object; (re)computed if missing or stale."""
    st = target.stat()
    mf = meta_file(target)
    if mf.is_file():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            if data.get("size") == st.st_size and data.get("mtime") == st.st_mtime:
                return data
        except (ValueError, OSError):
            pass
    return write_meta(target, hash_md5(target), None, {})


# ---------------------------------------------------------------------------
# Request bodies: aws-chunked decoding + atomic streaming writes
# ---------------------------------------------------------------------------

async def decode_aws_chunked(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """
    Decode aws-chunked framing: `<hex-size>[;chunk-signature=...]\\r\\n<data>\\r\\n`
    repeated, then a 0-size chunk followed by optional trailer headers
    (x-amz-checksum-*). Chunk signatures and trailer checksums are not
    verified — this strips the framing so the stored bytes are the payload.
    """
    it = source.__aiter__()
    buf = bytearray()
    eof = False

    async def fill() -> bool:
        nonlocal eof
        if eof:
            return False
        try:
            buf.extend(await it.__anext__())
            return True
        except StopAsyncIteration:
            eof = True
            return False

    async def read_line() -> bytes:
        while True:
            i = buf.find(b"\r\n")
            if i >= 0:
                line = bytes(buf[:i])
                del buf[: i + 2]
                return line
            if not await fill():
                raise S3Error(400, "IncompleteBody", "Truncated aws-chunked payload.")

    while True:
        line = await read_line()
        if not line:
            continue
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise S3Error(400, "InvalidChunkSizeError", "Malformed aws-chunked chunk header.")
        if size == 0:
            break
        remaining = size
        while remaining:
            if not buf and not await fill():
                raise S3Error(400, "IncompleteBody", "Truncated aws-chunked payload.")
            take = min(remaining, len(buf))
            yield bytes(buf[:take])
            del buf[:take]
            remaining -= take
        while len(buf) < 2:
            if not await fill():
                raise S3Error(400, "IncompleteBody", "Truncated aws-chunked payload.")
        if buf[:2] != b"\r\n":
            raise S3Error(400, "InvalidChunkSizeError", "Missing CRLF after chunk data.")
        del buf[:2]

    while await fill():  # drain trailer headers after the final chunk
        pass


def body_stream(request: Request) -> AsyncIterator[bytes]:
    """The request payload stream, transparently unwrapping aws-chunked framing."""
    sha = request.headers.get("x-amz-content-sha256", "")
    encoding = request.headers.get("content-encoding", "")
    if sha.startswith("STREAMING-") or "aws-chunked" in encoding.lower():
        return decode_aws_chunked(request.stream())
    return request.stream()


async def save_body(request: Request, target: Path) -> tuple[int, str]:
    """
    Stream the request payload into a temp file, then atomically rename into
    place — an object appears fully written or not at all, like real S3.
    Returns (size, md5-hex); the MD5 doubles as the ETag.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}{TMP_SUFFIX}")
    digest = hashlib.md5()
    size = 0
    try:
        with tmp.open("wb") as out:
            async for chunk in body_stream(request):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


# ---------------------------------------------------------------------------
# Download helpers: streaming, Range, conditional requests
# ---------------------------------------------------------------------------

def stream_file(path: Path, start: int = 0, length: int | None = None) -> Iterator[bytes]:
    """Sync generator on purpose: Starlette iterates it in a worker thread."""
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while True:
            step = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if step == 0:
                break
            chunk = handle.read(step)
            if not chunk:
                break
            yield chunk
            if remaining is not None:
                remaining -= len(chunk)


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


def prune_empty_dirs(directory: Path, stop_at: Path) -> None:
    """Remove empty folders left after deletes, walking up to the bucket root."""
    try:
        directory, stop_at = directory.resolve(), stop_at.resolve()
        while directory != stop_at and directory.is_relative_to(stop_at):
            directory.rmdir()  # OSError when non-empty — our stop signal
            directory = directory.parent
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Service operations
# ---------------------------------------------------------------------------

def created_time(path: Path) -> float:
    """File creation time (st_birthtime where available, else st_ctime)."""
    st = path.stat()
    return getattr(st, "st_birthtime", None) or st.st_ctime


def op_list_buckets() -> Response:
    if SINGLE_BUCKET:
        rows = [
            f"<Bucket><Name>{x(BUCKET_DISPLAY_NAME)}</Name>"
            f"<CreationDate>{s3_time(created_time(STORAGE_ROOT))}</CreationDate></Bucket>"
        ]
    else:
        rows = []
        for entry in sorted(STORAGE_ROOT.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                rows.append(
                    f"<Bucket><Name>{x(entry.name)}</Name>"
                    f"<CreationDate>{s3_time(created_time(entry))}</CreationDate></Bucket>"
                )
    return xml_response(
        f'<ListAllMyBucketsResult xmlns="{XMLNS}">'
        "<Owner><ID>fake-s3</ID><DisplayName>fake-s3</DisplayName></Owner>"
        f"<Buckets>{''.join(rows)}</Buckets></ListAllMyBucketsResult>"
    )


# ---------------------------------------------------------------------------
# Bucket operations
# ---------------------------------------------------------------------------

def op_create_bucket(bucket: str) -> Response:
    # Re-creating an existing bucket returns 200, matching us-east-1 behavior.
    bucket_dir(bucket).mkdir(parents=True, exist_ok=True)
    return Response(status_code=200, headers={"Location": f"/{bucket}"})


def op_head_bucket(bucket: str) -> Response:
    require_bucket(bucket)
    return Response(status_code=200, headers={"x-amz-bucket-region": REGION})


def op_delete_bucket(bucket: str) -> Response:
    d = require_bucket(bucket)
    if collect_objects(d):
        raise S3Error(409, "BucketNotEmpty", "The bucket you tried to delete is not empty.")
    if not SINGLE_BUCKET:  # never remove the storage root itself
        shutil.rmtree(d)
    return Response(status_code=204)


def op_bucket_location(bucket: str) -> Response:
    require_bucket(bucket)
    # us-east-1 is reported as an empty LocationConstraint, like real S3.
    constraint = "" if REGION == "us-east-1" else x(REGION)
    return xml_response(f'<LocationConstraint xmlns="{XMLNS}">{constraint}</LocationConstraint>')


def collect_objects(bdir: Path) -> list[tuple[str, Path]]:
    """
    All (key, path) pairs in a bucket, sorted by key string (S3 byte order,
    which differs from Path component order). Empty directories are surfaced
    as zero-byte "prefix/" marker objects.
    """
    items: list[tuple[str, Path]] = []
    for entry in bdir.rglob("*"):
        rel = entry.relative_to(bdir)
        if rel.parts and rel.parts[0].startswith(".fake-s3"):
            continue  # hidden bookkeeping (multipart uploads) under the root
        if entry.is_file() and not entry.name.endswith(RESERVED_SUFFIXES):
            items.append((rel.as_posix(), entry))
        elif entry.is_dir():
            has_files = any(
                f.is_file() and not f.name.endswith(RESERVED_SUFFIXES)
                for f in entry.rglob("*")
            )
            if not has_files:
                items.append((rel.as_posix() + "/", entry))
    items.sort(key=lambda pair: pair[0])
    return items


def contents_xml(key: str, path: Path, url_encode: bool) -> str:
    if path.is_dir():  # empty-directory marker object
        size, mtime, etag = 0, path.stat().st_mtime, EMPTY_MD5
    else:
        st = path.stat()
        size, mtime, etag = st.st_size, st.st_mtime, object_meta(path)["etag"]
    shown = quote(key, safe="/") if url_encode else key
    return (
        f"<Contents><Key>{x(shown)}</Key><LastModified>{s3_time(mtime)}</LastModified>"
        f"<ETag>&quot;{x(etag)}&quot;</ETag><Size>{size}</Size>"
        "<StorageClass>STANDARD</StorageClass></Contents>"
    )


def op_list_objects(bucket: str, q: QueryParams, v2: bool) -> Response:
    bdir = require_bucket(bucket)
    prefix = q.get("prefix", "")
    delimiter = q.get("delimiter", "")
    encoding = q.get("encoding-type", "")
    url_encode = encoding == "url"
    try:
        max_keys = max(0, min(1000, int(q.get("max-keys") or "1000")))
    except ValueError:
        raise S3Error(400, "InvalidArgument", "Invalid max-keys value.")

    if v2:
        token = q.get("continuation-token")
        if token:
            try:
                after = base64.b64decode(token.encode()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise S3Error(400, "InvalidArgument", "The continuation token provided is incorrect.")
        else:
            after = q.get("start-after", "")
    else:
        after = q.get("marker", "")

    # Build the merged, sorted stream of objects and common prefixes.
    items: list[tuple[str, str, Path | None]] = []  # (kind, key-or-prefix, path)
    seen_prefixes: set[str] = set()
    for key, path in collect_objects(bdir):
        if prefix and not key.startswith(prefix):
            continue
        if delimiter:
            rest = key[len(prefix):]
            if delimiter in rest:
                cp = prefix + rest.split(delimiter, 1)[0] + delimiter
                if cp not in seen_prefixes:
                    seen_prefixes.add(cp)
                    items.append(("cp", cp, None))
                continue
        items.append(("obj", key, path))

    if after:
        # A common-prefix marker means "skip everything under that prefix" too.
        items = [
            it for it in items
            if it[1] > after
            and not (delimiter and after.endswith(delimiter) and it[1].startswith(after))
        ]

    truncated = len(items) > max_keys
    page = items[:max_keys]
    last = page[-1][1] if page else ""

    def enc(value: str) -> str:
        return quote(value, safe="/") if url_encode else value

    parts = [
        f'<ListBucketResult xmlns="{XMLNS}">',
        f"<Name>{x(bucket)}</Name>",
        f"<Prefix>{x(enc(prefix))}</Prefix>",
        f"<MaxKeys>{max_keys}</MaxKeys>",
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
    ]
    if delimiter:
        parts.append(f"<Delimiter>{x(enc(delimiter))}</Delimiter>")
    if url_encode:
        parts.append("<EncodingType>url</EncodingType>")
    if v2:
        parts.append(f"<KeyCount>{len(page)}</KeyCount>")
        if q.get("continuation-token"):
            parts.append(f"<ContinuationToken>{x(q.get('continuation-token'))}</ContinuationToken>")
        if q.get("start-after"):
            parts.append(f"<StartAfter>{x(enc(q.get('start-after')))}</StartAfter>")
        if truncated:
            next_token = base64.b64encode(last.encode("utf-8")).decode()
            parts.append(f"<NextContinuationToken>{x(next_token)}</NextContinuationToken>")
    else:
        parts.append(f"<Marker>{x(enc(q.get('marker', '')))}</Marker>")
        if truncated and delimiter:
            parts.append(f"<NextMarker>{x(enc(last))}</NextMarker>")

    for kind, value, path in page:
        if kind == "obj":
            parts.append(contents_xml(value, path, url_encode))
        else:
            parts.append(f"<CommonPrefixes><Prefix>{x(enc(value))}</Prefix></CommonPrefixes>")
    parts.append("</ListBucketResult>")
    return xml_response("".join(parts))


def delete_single(bucket: str, key: str, is_marker: bool) -> None:
    """Delete one object (idempotent, like S3 DeleteObject)."""
    bdir = require_bucket(bucket)
    target = resolve_safe_path(bucket, key)
    if is_marker:
        if target.is_dir():
            try:
                target.rmdir()
                prune_empty_dirs(target.parent, bdir)
            except OSError:
                pass  # non-empty: the "marker" is implicit, nothing to delete
        return
    if target.is_file():
        target.unlink(missing_ok=True)
        meta_file(target).unlink(missing_ok=True)
        prune_empty_dirs(target.parent, bdir)


async def op_delete_objects(request: Request, bucket: str) -> Response:
    require_bucket(bucket)
    root = parse_xml(await request.body())
    quiet = any(
        local_name(el.tag) == "Quiet" and (el.text or "").strip().lower() == "true"
        for el in root.iter()
    )
    results = []
    for obj in (el for el in root.iter() if local_name(el.tag) == "Object"):
        raw_key = next(
            (c.text or "" for c in obj if local_name(c.tag) == "Key"), ""
        )
        try:
            key, is_marker = normalize_key(raw_key)
            delete_single(bucket, key, is_marker)
            if not quiet:
                results.append(f"<Deleted><Key>{x(raw_key)}</Key></Deleted>")
        except S3Error as exc:
            results.append(
                f"<Error><Key>{x(raw_key)}</Key><Code>{x(exc.code)}</Code>"
                f"<Message>{x(exc.message)}</Message></Error>"
            )
    return xml_response(f'<DeleteResult xmlns="{XMLNS}">{"".join(results)}</DeleteResult>')


# ---------------------------------------------------------------------------
# Object operations
# ---------------------------------------------------------------------------

def user_metadata(request: Request) -> dict:
    return {
        name[len("x-amz-meta-"):]: value
        for name, value in request.headers.items()
        if name.startswith("x-amz-meta-")
    }


async def op_put_object(request: Request, bucket: str, key: str, is_marker: bool) -> Response:
    ensure_bucket(bucket)
    target = resolve_safe_path(bucket, key)
    if is_marker:
        async for _ in body_stream(request):  # drain (marker bodies are empty)
            pass
        target.mkdir(parents=True, exist_ok=True)
        return Response(status_code=200, headers={"ETag": f'"{EMPTY_MD5}"'})
    _, etag = await save_body(request, target)
    write_meta(target, etag, request.headers.get("content-type"), user_metadata(request))
    return Response(status_code=200, headers={"ETag": f'"{etag}"'})


def parse_copy_source(request: Request) -> tuple[str, str]:
    source = unquote(request.headers.get("x-amz-copy-source", "")).split("?", 1)[0]
    bucket, _, raw_key = source.lstrip("/").partition("/")
    if not bucket or not raw_key:
        raise S3Error(400, "InvalidArgument", "Invalid x-amz-copy-source header.")
    key, is_marker = normalize_key(raw_key)
    if is_marker:
        raise S3Error(400, "InvalidArgument", "Cannot copy a directory marker.")
    return bucket, key


async def op_copy_object(request: Request, bucket: str, key: str) -> Response:
    src_bucket, src_key = parse_copy_source(request)
    require_bucket(src_bucket)
    source = resolve_safe_path(src_bucket, src_key)
    if not source.is_file():
        raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
    ensure_bucket(bucket)
    target = resolve_safe_path(bucket, key)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}{TMP_SUFFIX}")
    try:
        shutil.copyfile(source, tmp)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # CopyObject always yields a single-part ETag (plain MD5), even when the
    # source was a multipart upload — same as real S3.
    etag = hash_md5(target)

    if request.headers.get("x-amz-metadata-directive", "COPY").upper() == "REPLACE":
        content_type = request.headers.get("content-type")
        meta = user_metadata(request)
    else:
        src_meta = object_meta(source)
        content_type = src_meta.get("content_type")
        meta = src_meta.get("meta") or {}
    write_meta(target, etag, content_type, meta)

    return xml_response(
        f'<CopyObjectResult xmlns="{XMLNS}">'
        f"<LastModified>{s3_time(target.stat().st_mtime)}</LastModified>"
        f"<ETag>&quot;{x(etag)}&quot;</ETag></CopyObjectResult>"
    )


def op_get_object(request: Request, bucket: str, key: str, is_marker: bool,
                  q: QueryParams, head: bool) -> Response:
    require_bucket(bucket)
    target = resolve_safe_path(bucket, key)

    if is_marker:
        if not target.is_dir():
            raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
        return Response(status_code=200, headers={
            "ETag": f'"{EMPTY_MD5}"',
            "Content-Type": "application/x-directory",
            "Content-Length": "0",
            "Last-Modified": http_date(target.stat().st_mtime),
            "Accept-Ranges": "bytes",
        })

    if not target.is_file():
        raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
    meta = object_meta(target)
    st = target.stat()
    etag = meta["etag"]

    not_modified = conditional_response(request, etag, st.st_mtime)
    if not_modified:
        return not_modified

    headers = {
        "ETag": f'"{etag}"',
        "Last-Modified": http_date(st.st_mtime),
        "Accept-Ranges": "bytes",
        "Content-Type": q.get("response-content-type")
                        or meta.get("content_type")
                        or guess_mime_type(target),
    }
    for name, value in (meta.get("meta") or {}).items():
        headers[f"x-amz-meta-{name}"] = value
    if q.get("response-content-disposition"):
        headers["Content-Disposition"] = q.get("response-content-disposition")
    if q.get("response-cache-control"):
        headers["Cache-Control"] = q.get("response-cache-control")

    if head:
        headers["Content-Length"] = str(st.st_size)
        return Response(status_code=200, headers=headers)

    range_header = request.headers.get("range")
    if range_header:
        start, end = parse_range(range_header, st.st_size)
        headers["Content-Range"] = f"bytes {start}-{end}/{st.st_size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(stream_file(target, start, end - start + 1),
                                 status_code=206, headers=headers)

    headers["Content-Length"] = str(st.st_size)
    return StreamingResponse(stream_file(target), status_code=200, headers=headers)


def op_delete_object(bucket: str, key: str, is_marker: bool) -> Response:
    delete_single(bucket, key, is_marker)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Multipart uploads
# ---------------------------------------------------------------------------

def manifest_file(upload_id: str) -> Path:
    return MULTIPART_ROOT / f"{upload_id}.json"


def part_file(upload_id: str, number: int) -> Path:
    return MULTIPART_ROOT / f"{upload_id}.{number:05d}.part"


def load_manifest(upload_id: str, bucket: str, key: str) -> dict:
    mf = manifest_file(upload_id)
    if not upload_id or not mf.is_file():
        raise S3Error(404, "NoSuchUpload", "The specified multipart upload does not exist.")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    if manifest.get("bucket") != bucket or manifest.get("key") != key:
        raise S3Error(404, "NoSuchUpload", "The specified multipart upload does not exist.")
    return manifest


def op_create_multipart(request: Request, bucket: str, key: str) -> Response:
    ensure_bucket(bucket)
    resolve_safe_path(bucket, key)  # validates the key maps inside the root
    MULTIPART_ROOT.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    manifest_file(upload_id).write_text(json.dumps({
        "bucket": bucket,
        "key": key,
        "content_type": request.headers.get("content-type"),
        "meta": user_metadata(request),
        "initiated": time.time(),
    }), encoding="utf-8")
    return xml_response(
        f'<InitiateMultipartUploadResult xmlns="{XMLNS}">'
        f"<Bucket>{x(bucket)}</Bucket><Key>{x(key)}</Key>"
        f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>"
    )


async def op_upload_part(request: Request, bucket: str, key: str, q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    load_manifest(upload_id, bucket, key)
    try:
        number = int(q.get("partNumber", ""))
        if not 1 <= number <= 10000:
            raise ValueError
    except ValueError:
        raise S3Error(400, "InvalidArgument",
                      "Part number must be an integer between 1 and 10000.")
    target = part_file(upload_id, number)
    _, etag = await save_body(request, target)
    # Parts upload concurrently, so each records its ETag in its own file —
    # no shared manifest rewrite that could race.
    target.with_suffix(target.suffix + ".etag").write_text(etag, encoding="utf-8")
    return Response(status_code=200, headers={"ETag": f'"{etag}"'})


async def op_complete_multipart(request: Request, bucket: str, key: str,
                                q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    manifest = load_manifest(upload_id, bucket, key)
    root = parse_xml(await request.body())

    requested: list[tuple[int, str]] = []
    for part_el in (el for el in root.iter() if local_name(el.tag) == "Part"):
        number, etag = None, ""
        for child in part_el:
            if local_name(child.tag) == "PartNumber":
                try:
                    number = int((child.text or "").strip())
                except ValueError:
                    raise S3Error(400, "MalformedXML", "Invalid PartNumber.")
            elif local_name(child.tag) == "ETag":
                etag = (child.text or "").strip().strip('"')
        if number is None:
            raise S3Error(400, "MalformedXML", "Part is missing a PartNumber.")
        requested.append((number, etag))
    if not requested:
        raise S3Error(400, "MalformedXML", "No parts specified.")
    numbers = [n for n, _ in requested]
    if numbers != sorted(set(numbers)):
        raise S3Error(400, "InvalidPartOrder",
                      "The list of parts was not in ascending order.")

    parts: list[tuple[int, Path, str]] = []
    for number, claimed_etag in requested:
        pf = part_file(upload_id, number)
        ef = pf.with_suffix(pf.suffix + ".etag")
        if not pf.is_file() or not ef.is_file():
            raise S3Error(400, "InvalidPart", f"Part {number} was not uploaded.")
        stored_etag = ef.read_text(encoding="utf-8").strip()
        if claimed_etag and claimed_etag != stored_etag:
            raise S3Error(400, "InvalidPart", f"ETag mismatch for part {number}.")
        parts.append((number, pf, stored_etag))

    ensure_bucket(bucket)
    target = resolve_safe_path(bucket, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}{TMP_SUFFIX}")
    try:
        with tmp.open("wb") as out:
            for _, pf, _ in parts:
                with pf.open("rb") as src:
                    shutil.copyfileobj(src, out, CHUNK_SIZE)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    # Multipart ETag, exactly like S3: md5 of the concatenated part MD5 digests,
    # suffixed with the part count.
    combined = hashlib.md5(b"".join(bytes.fromhex(e) for _, _, e in parts))
    etag = f"{combined.hexdigest()}-{len(parts)}"
    write_meta(target, etag, manifest.get("content_type"), manifest.get("meta") or {})
    cleanup_multipart(upload_id)

    host = request.headers.get("host", f"localhost:{PORT}")
    location = f"http://{host}/{quote(bucket)}/{quote(key, safe='/')}"
    return xml_response(
        f'<CompleteMultipartUploadResult xmlns="{XMLNS}">'
        f"<Location>{x(location)}</Location><Bucket>{x(bucket)}</Bucket>"
        f"<Key>{x(key)}</Key><ETag>&quot;{x(etag)}&quot;</ETag>"
        "</CompleteMultipartUploadResult>"
    )


def cleanup_multipart(upload_id: str) -> None:
    for leftover in MULTIPART_ROOT.glob(f"{upload_id}.*"):
        leftover.unlink(missing_ok=True)


def op_abort_multipart(bucket: str, key: str, q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    load_manifest(upload_id, bucket, key)
    cleanup_multipart(upload_id)
    return Response(status_code=204)


def op_list_parts(bucket: str, key: str, q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    load_manifest(upload_id, bucket, key)
    rows = []
    last_number = 0
    for pf in sorted(MULTIPART_ROOT.glob(f"{upload_id}.*.part")):
        number = int(pf.name.split(".")[-2])
        ef = pf.with_suffix(pf.suffix + ".etag")
        etag = ef.read_text(encoding="utf-8").strip() if ef.is_file() else ""
        st = pf.stat()
        last_number = number
        rows.append(
            f"<Part><PartNumber>{number}</PartNumber>"
            f"<LastModified>{s3_time(st.st_mtime)}</LastModified>"
            f"<ETag>&quot;{x(etag)}&quot;</ETag><Size>{st.st_size}</Size></Part>"
        )
    return xml_response(
        f'<ListPartsResult xmlns="{XMLNS}">'
        f"<Bucket>{x(bucket)}</Bucket><Key>{x(key)}</Key>"
        f"<UploadId>{upload_id}</UploadId>"
        "<StorageClass>STANDARD</StorageClass>"
        f"<PartNumberMarker>0</PartNumberMarker>"
        f"<NextPartNumberMarker>{last_number}</NextPartNumberMarker>"
        "<MaxParts>1000</MaxParts><IsTruncated>false</IsTruncated>"
        f"{''.join(rows)}</ListPartsResult>"
    )


def op_list_multipart_uploads(bucket: str) -> Response:
    require_bucket(bucket)
    rows = []
    if MULTIPART_ROOT.is_dir():
        for mf in sorted(MULTIPART_ROOT.glob("*.json")):
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if manifest.get("bucket") != bucket:
                continue
            rows.append(
                f"<Upload><Key>{x(manifest.get('key', ''))}</Key>"
                f"<UploadId>{mf.stem}</UploadId>"
                "<StorageClass>STANDARD</StorageClass>"
                f"<Initiated>{s3_time(manifest.get('initiated', time.time()))}</Initiated>"
                "</Upload>"
            )
    return xml_response(
        f'<ListMultipartUploadsResult xmlns="{XMLNS}">'
        f"<Bucket>{x(bucket)}</Bucket>"
        "<KeyMarker></KeyMarker><UploadIdMarker></UploadIdMarker>"
        "<MaxUploads>1000</MaxUploads><IsTruncated>false</IsTruncated>"
        f"{''.join(rows)}</ListMultipartUploadsResult>"
    )


# ---------------------------------------------------------------------------
# ACL / configuration stubs
# ---------------------------------------------------------------------------

XSI = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def acl_xml() -> Response:
    """Canned public-read ACL (flysystem reads this as visibility=public)."""
    return xml_response(
        f'<AccessControlPolicy xmlns="{XMLNS}">'
        "<Owner><ID>fake-s3</ID><DisplayName>fake-s3</DisplayName></Owner>"
        "<AccessControlList>"
        f'<Grant><Grantee {XSI} xsi:type="CanonicalUser">'
        "<ID>fake-s3</ID><DisplayName>fake-s3</DisplayName></Grantee>"
        "<Permission>FULL_CONTROL</Permission></Grant>"
        f'<Grant><Grantee {XSI} xsi:type="Group">'
        "<URI>http://acs.amazonaws.com/groups/global/AllUsers</URI></Grantee>"
        "<Permission>READ</Permission></Grant>"
        "</AccessControlList></AccessControlPolicy>"
    )


# Bucket configuration subresources that have no configuration by default:
# real S3 answers these GETs with specific 404 error codes.
MISSING_BUCKET_CONFIG = {
    "policy": ("NoSuchBucketPolicy", "The bucket policy does not exist."),
    "cors": ("NoSuchCORSConfiguration", "The CORS configuration does not exist."),
    "lifecycle": ("NoSuchLifecycleConfiguration", "The lifecycle configuration does not exist."),
    "tagging": ("NoSuchTagSet", "There is no tag set associated with the bucket."),
    "encryption": ("ServerSideEncryptionConfigurationNotFoundError",
                   "The server side encryption configuration was not found."),
    "website": ("NoSuchWebsiteConfiguration",
                "The specified bucket does not have a website configuration."),
    "replication": ("ReplicationConfigurationNotFoundError",
                    "The replication configuration was not found."),
    "publicAccessBlock": ("NoSuchPublicAccessBlockConfiguration",
                          "The public access block configuration was not found."),
    "object-lock": ("ObjectLockConfigurationNotFoundError",
                    "Object Lock configuration does not exist for this bucket."),
}

# Bucket configuration writes are accepted and ignored (dev convenience).
IGNORED_BUCKET_CONFIG_WRITES = {
    "acl", "versioning", "cors", "policy", "lifecycle", "tagging", "encryption",
    "website", "logging", "notification", "replication", "requestPayment",
    "publicAccessBlock", "ownershipControls", "object-lock", "accelerate",
    "intelligent-tiering", "analytics", "metrics", "inventory",
}


# ---------------------------------------------------------------------------
# Dispatch — the S3 protocol multiplexes operations onto method + query params
# ---------------------------------------------------------------------------

async def dispatch_bucket(request: Request, method: str, bucket: str,
                          q: QueryParams) -> Response:
    if method in ("GET", "HEAD") and method == "HEAD":
        return op_head_bucket(bucket)

    if method == "GET":
        if "location" in q:
            return op_bucket_location(bucket)
        if "uploads" in q:
            return op_list_multipart_uploads(bucket)
        if "acl" in q:
            require_bucket(bucket)
            return acl_xml()
        if "versioning" in q:
            require_bucket(bucket)
            return xml_response(f'<VersioningConfiguration xmlns="{XMLNS}"/>')
        if "notification" in q:
            require_bucket(bucket)
            return xml_response(f'<NotificationConfiguration xmlns="{XMLNS}"/>')
        if "logging" in q:
            require_bucket(bucket)
            return xml_response(f'<BucketLoggingStatus xmlns="{XMLNS}"/>')
        if "requestPayment" in q:
            require_bucket(bucket)
            return xml_response(
                f'<RequestPaymentConfiguration xmlns="{XMLNS}">'
                "<Payer>BucketOwner</Payer></RequestPaymentConfiguration>"
            )
        for name, (code, message) in MISSING_BUCKET_CONFIG.items():
            if name in q:
                require_bucket(bucket)
                raise S3Error(404, code, message)
        return op_list_objects(bucket, q, v2=q.get("list-type") == "2")

    if method == "PUT":
        if any(name in q for name in IGNORED_BUCKET_CONFIG_WRITES):
            require_bucket(bucket)
            return Response(status_code=200)
        return op_create_bucket(bucket)

    if method == "DELETE":
        if any(name in q for name in IGNORED_BUCKET_CONFIG_WRITES):
            require_bucket(bucket)
            return Response(status_code=204)
        return op_delete_bucket(bucket)

    if method == "POST":
        if "delete" in q:
            return await op_delete_objects(request, bucket)

    raise S3Error(501, "NotImplemented",
                  "This fake S3 server does not implement that bucket operation.")


async def dispatch_object(request: Request, method: str, bucket: str,
                          raw_key: str, q: QueryParams) -> Response:
    key, is_marker = normalize_key(raw_key)

    if method in ("GET", "HEAD"):
        if method == "GET" and "uploadId" in q:
            return op_list_parts(bucket, key, q)
        if method == "GET" and "acl" in q:
            require_bucket(bucket)
            return acl_xml()
        if method == "GET" and "tagging" in q:
            require_bucket(bucket)
            return xml_response(f'<Tagging xmlns="{XMLNS}"><TagSet/></Tagging>')
        if method == "GET" and "attributes" in q:
            raise S3Error(501, "NotImplemented", "GetObjectAttributes is not implemented.")
        return op_get_object(request, bucket, key, is_marker, q, head=(method == "HEAD"))

    if method == "PUT":
        if "partNumber" in q and "uploadId" in q:
            return await op_upload_part(request, bucket, key, q)
        if "acl" in q or "tagging" in q:
            require_bucket(bucket)
            return Response(status_code=200)
        if "x-amz-copy-source" in request.headers:
            return await op_copy_object(request, bucket, key)
        return await op_put_object(request, bucket, key, is_marker)

    if method == "POST":
        if "uploads" in q:
            return op_create_multipart(request, bucket, key)
        if "uploadId" in q:
            return await op_complete_multipart(request, bucket, key, q)

    if method == "DELETE":
        if "uploadId" in q:
            return op_abort_multipart(bucket, key, q)
        if "tagging" in q:
            require_bucket(bucket)
            return Response(status_code=204)
        return op_delete_object(bucket, key, is_marker)

    raise S3Error(501, "NotImplemented",
                  "This fake S3 server does not implement that object operation.")


@app.api_route("/{rest:path}", methods=["GET", "HEAD", "PUT", "POST", "DELETE"])
async def dispatch(request: Request, rest: str) -> Response:
    method = request.method.upper()
    q = request.query_params
    check_presigned_expiry(q)

    hosted_bucket = vhost_bucket(request)
    if hosted_bucket is not None:
        bucket, raw_key = hosted_bucket, rest
    else:
        bucket, _, raw_key = rest.partition("/")

    if not bucket:
        if method == "GET":
            return op_list_buckets()
        raise S3Error(405, "MethodNotAllowed",
                      "The specified method is not allowed against this resource.")

    # Reserved liveness probe (see module docstring).
    if bucket == "health" and not raw_key and method == "GET":
        return JSONResponse({"status": "ok", "storage_root": str(STORAGE_ROOT)})

    if not BUCKET_RE.fullmatch(bucket):
        raise S3Error(400, "InvalidBucketName", "The specified bucket is not valid.")

    if not raw_key:
        return await dispatch_bucket(request, method, bucket, q)
    return await dispatch_object(request, method, bucket, raw_key, q)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
