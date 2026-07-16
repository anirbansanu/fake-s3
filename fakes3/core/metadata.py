"""Object metadata sidecars (etag cache, content-type, x-amz-meta-*)."""

import hashlib
import json
from pathlib import Path

from .keys import CHUNK_SIZE, META_SUFFIX


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
