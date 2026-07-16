"""Multipart upload operations."""

import hashlib
import json
import shutil
import time
import uuid
from urllib.parse import quote
from xml.sax.saxutils import escape as x

from fastapi import Request, Response
from starlette.datastructures import QueryParams

from ..core.engine import StorageEngine
from ..core.errors import S3Error
from ..core.keys import CHUNK_SIZE, TMP_SUFFIX
from ..core.metadata import write_meta
from .chunked import body_stream
from .httputil import user_metadata
from .xmlgen import XMLNS, local_name, parse_xml, s3_time, xml_response


def load_manifest(engine: StorageEngine, upload_id: str, bucket: str, key: str) -> dict:
    mf = engine.manifest_file(upload_id)
    if not upload_id or not mf.is_file():
        raise S3Error(404, "NoSuchUpload", "The specified multipart upload does not exist.")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    if manifest.get("bucket") != bucket or manifest.get("key") != key:
        raise S3Error(404, "NoSuchUpload", "The specified multipart upload does not exist.")
    return manifest


def op_create_multipart(engine: StorageEngine, request: Request, bucket: str, key: str) -> Response:
    engine.ensure_bucket(bucket)
    engine.resolve_safe_path(bucket, key)  # validates the key maps inside the root
    engine.multipart_root.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    engine.manifest_file(upload_id).write_text(json.dumps({
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


async def op_upload_part(engine: StorageEngine, request: Request, bucket: str,
                         key: str, q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    load_manifest(engine, upload_id, bucket, key)
    try:
        number = int(q.get("partNumber", ""))
        if not 1 <= number <= 10000:
            raise ValueError
    except ValueError:
        raise S3Error(400, "InvalidArgument",
                      "Part number must be an integer between 1 and 10000.")
    target = engine.part_file(upload_id, number)
    _, etag = await engine.save_stream(body_stream(request), target)
    # Parts upload concurrently, so each records its ETag in its own file —
    # no shared manifest rewrite that could race.
    target.with_suffix(target.suffix + ".etag").write_text(etag, encoding="utf-8")
    return Response(status_code=200, headers={"ETag": f'"{etag}"'})


async def op_complete_multipart(engine: StorageEngine, request: Request, bucket: str,
                                key: str, q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    manifest = load_manifest(engine, upload_id, bucket, key)
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

    parts = []
    for number, claimed_etag in requested:
        pf = engine.part_file(upload_id, number)
        ef = pf.with_suffix(pf.suffix + ".etag")
        if not pf.is_file() or not ef.is_file():
            raise S3Error(400, "InvalidPart", f"Part {number} was not uploaded.")
        stored_etag = ef.read_text(encoding="utf-8").strip()
        if claimed_etag and claimed_etag != stored_etag:
            raise S3Error(400, "InvalidPart", f"ETag mismatch for part {number}.")
        parts.append((number, pf, stored_etag))

    engine.ensure_bucket(bucket)
    target = engine.resolve_safe_path(bucket, key)
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
    engine.cleanup_multipart(upload_id)

    host = request.headers.get("host", f"localhost:{engine.config.port}")
    location = f"http://{host}/{quote(bucket)}/{quote(key, safe='/')}"
    return xml_response(
        f'<CompleteMultipartUploadResult xmlns="{XMLNS}">'
        f"<Location>{x(location)}</Location><Bucket>{x(bucket)}</Bucket>"
        f"<Key>{x(key)}</Key><ETag>&quot;{x(etag)}&quot;</ETag>"
        "</CompleteMultipartUploadResult>"
    )


def op_abort_multipart(engine: StorageEngine, bucket: str, key: str, q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    load_manifest(engine, upload_id, bucket, key)
    engine.cleanup_multipart(upload_id)
    return Response(status_code=204)


def op_list_parts(engine: StorageEngine, bucket: str, key: str, q: QueryParams) -> Response:
    upload_id = q.get("uploadId", "")
    load_manifest(engine, upload_id, bucket, key)
    rows = []
    last_number = 0
    for pf in sorted(engine.multipart_root.glob(f"{upload_id}.*.part")):
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


def op_list_multipart_uploads(engine: StorageEngine, bucket: str) -> Response:
    engine.require_bucket(bucket)
    rows = []
    if engine.multipart_root.is_dir():
        for mf in sorted(engine.multipart_root.glob("*.json")):
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
