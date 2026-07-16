"""Object-key normalization, naming rules, and shared storage constants."""

import mimetypes
import posixpath
import re
from pathlib import Path

from .errors import S3Error

CHUNK_SIZE = 1024 * 1024  # 1 MB streaming chunk size
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # md5 of zero bytes
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Hidden bookkeeping files, never exposed as objects.
TMP_SUFFIX = ".fake-s3-part"          # in-progress atomic writes
META_SUFFIX = ".fake-s3-meta"         # per-object sidecar (etag, content-type, metadata)
RESERVED_SUFFIXES = (TMP_SUFFIX, META_SUFFIX)


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


def guess_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"
