"""
S3 operation handlers and dispatch.

The S3 protocol multiplexes operations onto method + query params; dispatch()
at the bottom routes every request. All handlers take the StorageEngine that
owns the filesystem layout — no module-level state.
"""

import base64
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape as x

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import QueryParams

from ..core.engine import StorageEngine, created_time, stream_file
from ..core.errors import S3Error
from ..core.keys import BUCKET_RE, EMPTY_MD5, guess_mime_type, normalize_key
from ..core.metadata import meta_file, hash_md5, object_meta, write_meta
from . import multipart
from .chunked import body_stream
from .httputil import (check_presigned_expiry, conditional_response, parse_copy_source,
                       parse_range, user_metadata, vhost_bucket)
from .xmlgen import XMLNS, http_date, local_name, parse_xml, s3_time, xml_response


# ---------------------------------------------------------------------------
# Service operations
# ---------------------------------------------------------------------------

def op_list_buckets(engine: StorageEngine) -> Response:
    if engine.config.single_bucket:
        rows = [
            f"<Bucket><Name>{x(engine.config.bucket_name)}</Name>"
            f"<CreationDate>{s3_time(created_time(engine.storage_root))}</CreationDate></Bucket>"
        ]
    else:
        rows = [
            f"<Bucket><Name>{x(entry.name)}</Name>"
            f"<CreationDate>{s3_time(created_time(entry))}</CreationDate></Bucket>"
            for entry in engine.list_bucket_dirs()
        ]
    return xml_response(
        f'<ListAllMyBucketsResult xmlns="{XMLNS}">'
        "<Owner><ID>fake-s3</ID><DisplayName>fake-s3</DisplayName></Owner>"
        f"<Buckets>{''.join(rows)}</Buckets></ListAllMyBucketsResult>"
    )


# ---------------------------------------------------------------------------
# Bucket operations
# ---------------------------------------------------------------------------

def op_create_bucket(engine: StorageEngine, bucket: str) -> Response:
    # Re-creating an existing bucket returns 200, matching us-east-1 behavior.
    engine.bucket_dir(bucket).mkdir(parents=True, exist_ok=True)
    return Response(status_code=200, headers={"Location": f"/{bucket}"})


def op_head_bucket(engine: StorageEngine, bucket: str) -> Response:
    engine.require_bucket(bucket)
    return Response(status_code=200, headers={"x-amz-bucket-region": engine.config.region})


def op_delete_bucket(engine: StorageEngine, bucket: str) -> Response:
    d = engine.require_bucket(bucket)
    if engine.collect_objects(d):
        raise S3Error(409, "BucketNotEmpty", "The bucket you tried to delete is not empty.")
    if not engine.config.single_bucket:  # never remove the storage root itself
        import shutil
        shutil.rmtree(d)
    return Response(status_code=204)


def op_bucket_location(engine: StorageEngine, bucket: str) -> Response:
    engine.require_bucket(bucket)
    # us-east-1 is reported as an empty LocationConstraint, like real S3.
    constraint = "" if engine.config.region == "us-east-1" else x(engine.config.region)
    return xml_response(f'<LocationConstraint xmlns="{XMLNS}">{constraint}</LocationConstraint>')


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


def op_list_objects(engine: StorageEngine, bucket: str, q: QueryParams, v2: bool) -> Response:
    bdir = engine.require_bucket(bucket)
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
    for key, path in engine.collect_objects(bdir):
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


async def op_delete_objects(engine: StorageEngine, request: Request, bucket: str) -> Response:
    engine.require_bucket(bucket)
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
            engine.delete_single(bucket, key, is_marker)
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

async def op_put_object(engine: StorageEngine, request: Request, bucket: str,
                        key: str, is_marker: bool) -> Response:
    engine.ensure_bucket(bucket)
    target = engine.resolve_safe_path(bucket, key)
    if is_marker:
        async for _ in body_stream(request):  # drain (marker bodies are empty)
            pass
        target.mkdir(parents=True, exist_ok=True)
        return Response(status_code=200, headers={"ETag": f'"{EMPTY_MD5}"'})
    _, etag = await engine.save_stream(body_stream(request), target)
    write_meta(target, etag, request.headers.get("content-type"), user_metadata(request))
    return Response(status_code=200, headers={"ETag": f'"{etag}"'})


async def op_copy_object(engine: StorageEngine, request: Request, bucket: str, key: str) -> Response:
    src_bucket, src_key = parse_copy_source(request)
    engine.require_bucket(src_bucket)
    source = engine.resolve_safe_path(src_bucket, src_key)
    if not source.is_file():
        raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
    engine.ensure_bucket(bucket)
    target = engine.resolve_safe_path(bucket, key)

    engine.copy_file(source, target)
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


def op_get_object(engine: StorageEngine, request: Request, bucket: str, key: str,
                  is_marker: bool, q: QueryParams, head: bool) -> Response:
    engine.require_bucket(bucket)
    target = engine.resolve_safe_path(bucket, key)

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


def op_delete_object(engine: StorageEngine, bucket: str, key: str, is_marker: bool) -> Response:
    engine.delete_single(bucket, key, is_marker)
    return Response(status_code=204)


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
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch_bucket(engine: StorageEngine, request: Request, method: str,
                          bucket: str, q: QueryParams) -> Response:
    if method == "HEAD":
        return op_head_bucket(engine, bucket)

    if method == "GET":
        if "location" in q:
            return op_bucket_location(engine, bucket)
        if "uploads" in q:
            return multipart.op_list_multipart_uploads(engine, bucket)
        if "acl" in q:
            engine.require_bucket(bucket)
            return acl_xml()
        if "versioning" in q:
            engine.require_bucket(bucket)
            return xml_response(f'<VersioningConfiguration xmlns="{XMLNS}"/>')
        if "notification" in q:
            engine.require_bucket(bucket)
            return xml_response(f'<NotificationConfiguration xmlns="{XMLNS}"/>')
        if "logging" in q:
            engine.require_bucket(bucket)
            return xml_response(f'<BucketLoggingStatus xmlns="{XMLNS}"/>')
        if "requestPayment" in q:
            engine.require_bucket(bucket)
            return xml_response(
                f'<RequestPaymentConfiguration xmlns="{XMLNS}">'
                "<Payer>BucketOwner</Payer></RequestPaymentConfiguration>"
            )
        for name, (code, message) in MISSING_BUCKET_CONFIG.items():
            if name in q:
                engine.require_bucket(bucket)
                raise S3Error(404, code, message)
        return op_list_objects(engine, bucket, q, v2=q.get("list-type") == "2")

    if method == "PUT":
        if any(name in q for name in IGNORED_BUCKET_CONFIG_WRITES):
            engine.require_bucket(bucket)
            return Response(status_code=200)
        return op_create_bucket(engine, bucket)

    if method == "DELETE":
        if any(name in q for name in IGNORED_BUCKET_CONFIG_WRITES):
            engine.require_bucket(bucket)
            return Response(status_code=204)
        return op_delete_bucket(engine, bucket)

    if method == "POST":
        if "delete" in q:
            return await op_delete_objects(engine, request, bucket)

    raise S3Error(501, "NotImplemented",
                  "This fake S3 server does not implement that bucket operation.")


async def dispatch_object(engine: StorageEngine, request: Request, method: str,
                          bucket: str, raw_key: str, q: QueryParams) -> Response:
    key, is_marker = normalize_key(raw_key)

    if method in ("GET", "HEAD"):
        if method == "GET" and "uploadId" in q:
            return multipart.op_list_parts(engine, bucket, key, q)
        if method == "GET" and "acl" in q:
            engine.require_bucket(bucket)
            return acl_xml()
        if method == "GET" and "tagging" in q:
            engine.require_bucket(bucket)
            return xml_response(f'<Tagging xmlns="{XMLNS}"><TagSet/></Tagging>')
        if method == "GET" and "attributes" in q:
            raise S3Error(501, "NotImplemented", "GetObjectAttributes is not implemented.")
        return op_get_object(engine, request, bucket, key, is_marker, q, head=(method == "HEAD"))

    if method == "PUT":
        if "partNumber" in q and "uploadId" in q:
            return await multipart.op_upload_part(engine, request, bucket, key, q)
        if "acl" in q or "tagging" in q:
            engine.require_bucket(bucket)
            return Response(status_code=200)
        if "x-amz-copy-source" in request.headers:
            return await op_copy_object(engine, request, bucket, key)
        return await op_put_object(engine, request, bucket, key, is_marker)

    if method == "POST":
        if "uploads" in q:
            return multipart.op_create_multipart(engine, request, bucket, key)
        if "uploadId" in q:
            return await multipart.op_complete_multipart(engine, request, bucket, key, q)

    if method == "DELETE":
        if "uploadId" in q:
            return multipart.op_abort_multipart(engine, bucket, key, q)
        if "tagging" in q:
            engine.require_bucket(bucket)
            return Response(status_code=204)
        return op_delete_object(engine, bucket, key, is_marker)

    raise S3Error(501, "NotImplemented",
                  "This fake S3 server does not implement that object operation.")


async def dispatch(engine: StorageEngine, request: Request, rest: str) -> Response:
    method = request.method.upper()
    q = request.query_params
    check_presigned_expiry(q)

    hosted_bucket = vhost_bucket(request, engine.config.vhost_bases)
    if hosted_bucket is not None:
        bucket, raw_key = hosted_bucket, rest
    else:
        bucket, _, raw_key = rest.partition("/")

    if not bucket:
        if method == "GET":
            return op_list_buckets(engine)
        raise S3Error(405, "MethodNotAllowed",
                      "The specified method is not allowed against this resource.")

    # Reserved liveness probe (a bucket literally named "health" cannot be
    # listed via GET; see project README).
    if bucket == "health" and not raw_key and method == "GET":
        return JSONResponse({"status": "ok", "storage_root": str(engine.storage_root)})

    if not BUCKET_RE.fullmatch(bucket):
        raise S3Error(400, "InvalidBucketName", "The specified bucket is not valid.")

    if not raw_key:
        return await dispatch_bucket(engine, request, method, bucket, q)
    return await dispatch_object(engine, request, method, bucket, raw_key, q)
