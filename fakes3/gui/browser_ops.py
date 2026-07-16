"""
Object-browser operations, built directly on the shared StorageEngine.

The GUI hosts the server in-process, so browsing works even while the HTTP
listener is stopped — same storage layout, same metadata sidecars, no
duplicate business logic.
"""

import shutil
from pathlib import Path

from ..core.engine import StorageEngine
from ..core.errors import S3Error
from ..core.keys import BUCKET_RE, guess_mime_type, normalize_key
from ..core.metadata import hash_md5, meta_file, object_meta, write_meta


def list_buckets(engine: StorageEngine) -> list[str]:
    if engine.config.single_bucket:
        return [engine.config.bucket_name]
    return [entry.name for entry in engine.list_bucket_dirs()]


def list_objects(engine: StorageEngine, bucket: str) -> list[dict]:
    bdir = engine.require_bucket(bucket)
    rows = []
    for key, path in engine.collect_objects(bdir):
        if path.is_dir():
            rows.append({"key": key, "size": 0, "mtime": path.stat().st_mtime,
                         "is_marker": True})
        else:
            st = path.stat()
            rows.append({"key": key, "size": st.st_size, "mtime": st.st_mtime,
                         "is_marker": False})
    return rows


def create_bucket(engine: StorageEngine, bucket: str) -> None:
    if not BUCKET_RE.fullmatch(bucket):
        raise S3Error(400, "InvalidBucketName",
                      "Bucket names are 3-63 chars: lowercase letters, digits, '.', '-'.")
    engine.bucket_dir(bucket).mkdir(parents=True, exist_ok=True)


def delete_bucket(engine: StorageEngine, bucket: str) -> None:
    d = engine.require_bucket(bucket)
    if engine.config.single_bucket:
        raise S3Error(400, "InvalidRequest",
                      "The shared bucket cannot be deleted in single-bucket mode.")
    shutil.rmtree(d)


def rename_bucket(engine: StorageEngine, bucket: str, new_name: str) -> None:
    if engine.config.single_bucket:
        raise S3Error(400, "InvalidRequest",
                      "Buckets cannot be renamed in single-bucket mode.")
    if not BUCKET_RE.fullmatch(new_name):
        raise S3Error(400, "InvalidBucketName",
                      "Bucket names are 3-63 chars: lowercase letters, digits, '.', '-'.")
    source = engine.require_bucket(bucket)
    target = engine.bucket_dir(new_name)
    if target.exists():
        raise S3Error(409, "BucketAlreadyExists", f"Bucket {new_name} already exists.")
    source.rename(target)


def upload(engine: StorageEngine, bucket: str, key: str, source: Path) -> None:
    key, is_marker = normalize_key(key)
    if is_marker:
        raise S3Error(400, "InvalidArgument", "Upload key may not end with '/'.")
    engine.ensure_bucket(bucket)
    target = engine.resolve_safe_path(bucket, key)
    engine.copy_file(Path(source), target)
    write_meta(target, hash_md5(target), guess_mime_type(target), {})


def download(engine: StorageEngine, bucket: str, key: str, dest: Path) -> None:
    engine.require_bucket(bucket)
    source = engine.resolve_safe_path(bucket, key)
    if not source.is_file():
        raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)


def delete_object(engine: StorageEngine, bucket: str, raw_key: str) -> None:
    key, is_marker = normalize_key(raw_key)
    engine.delete_single(bucket, key, is_marker)


def copy_object(engine: StorageEngine, bucket: str, src_key: str, dst_key: str) -> None:
    engine.require_bucket(bucket)
    dst_key, is_marker = normalize_key(dst_key)
    if is_marker:
        raise S3Error(400, "InvalidArgument", "Destination key may not end with '/'.")
    source = engine.resolve_safe_path(bucket, src_key)
    if not source.is_file():
        raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
    target = engine.resolve_safe_path(bucket, dst_key)
    engine.copy_file(source, target)
    src_meta = object_meta(source)
    write_meta(target, hash_md5(target), src_meta.get("content_type"),
               src_meta.get("meta") or {})


def move_object(engine: StorageEngine, bucket: str, src_key: str, dst_key: str) -> None:
    copy_object(engine, bucket, src_key, dst_key)
    key, _ = normalize_key(src_key)
    engine.delete_single(bucket, key, False)


def new_folder(engine: StorageEngine, bucket: str, name: str) -> None:
    key, _ = normalize_key(name if name.endswith("/") else name + "/")
    engine.ensure_bucket(bucket)
    engine.resolve_safe_path(bucket, key).mkdir(parents=True, exist_ok=True)


def head_object(engine: StorageEngine, bucket: str, raw_key: str) -> dict:
    engine.require_bucket(bucket)
    key, is_marker = normalize_key(raw_key)
    target = engine.resolve_safe_path(bucket, key)
    if is_marker:
        if not target.is_dir():
            raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
        return {"key": raw_key, "size": 0, "etag": "", "content_type":
                "application/x-directory", "mtime": target.stat().st_mtime, "meta": {}}
    if not target.is_file():
        raise S3Error(404, "NoSuchKey", "The specified key does not exist.")
    meta = object_meta(target)
    st = target.stat()
    return {
        "key": key,
        "size": st.st_size,
        "etag": meta["etag"],
        "content_type": meta.get("content_type") or guess_mime_type(target),
        "mtime": st.st_mtime,
        "meta": meta.get("meta") or {},
        "path": str(target),
        "sidecar": str(meta_file(target)),
    }
