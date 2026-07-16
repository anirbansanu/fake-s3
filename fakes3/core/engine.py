"""
StorageEngine — the filesystem-backed object store.

Owns the storage layout (bucket directories, sidecars, multipart workspace)
and every direct filesystem operation. No HTTP knowledge: the server layer
and the GUI browser both drive this same engine.
"""

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterator

from ..config import ServerConfig
from .errors import S3Error
from .keys import CHUNK_SIZE, RESERVED_SUFFIXES, TMP_SUFFIX
from .metadata import meta_file


def created_time(path: Path) -> float:
    """File creation time (st_birthtime where available, else st_ctime)."""
    st = path.stat()
    return getattr(st, "st_birthtime", None) or st.st_ctime


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


class StorageEngine:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.storage_root = Path(config.storage_root).resolve()
        self.multipart_root = self.storage_root / ".fake-s3-multipart"
        self.storage_root.mkdir(parents=True, exist_ok=True)

    # -- bucket directories --------------------------------------------------

    def bucket_dir(self, bucket: str) -> Path:
        return self.storage_root if self.config.single_bucket else self.storage_root / bucket

    def require_bucket(self, bucket: str) -> Path:
        d = self.bucket_dir(bucket)
        if not d.is_dir():
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist.")
        return d

    def ensure_bucket(self, bucket: str) -> Path:
        """Bucket dir for write operations, honoring the auto-create setting."""
        d = self.bucket_dir(bucket)
        if not d.is_dir():
            if not self.config.auto_create:
                raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist.")
            d.mkdir(parents=True, exist_ok=True)
        return d

    def list_bucket_dirs(self) -> list[Path]:
        """Real bucket folders (multi-bucket mode)."""
        return sorted(
            entry for entry in self.storage_root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def resolve_safe_path(self, bucket: str, key: str) -> Path:
        """Absolute path for <bucket>/<key>, rejecting escapes from the root."""
        target = (self.bucket_dir(bucket) / key).resolve()
        if not target.is_relative_to(self.storage_root):
            raise S3Error(400, "InvalidArgument", "Invalid bucket or object key.")
        return target

    # -- object enumeration ----------------------------------------------------

    def collect_objects(self, bdir: Path) -> list[tuple[str, Path]]:
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

    # -- writes ---------------------------------------------------------------

    async def save_stream(self, source: AsyncIterator[bytes], target: Path) -> tuple[int, str]:
        """
        Stream a payload into a temp file, then atomically rename into place —
        an object appears fully written or not at all, like real S3.
        Returns (size, md5-hex); the MD5 doubles as the ETag.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}{TMP_SUFFIX}")
        digest = hashlib.md5()
        size = 0
        try:
            with tmp.open("wb") as out:
                async for chunk in source:
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()

    def copy_file(self, source: Path, target: Path) -> None:
        """Atomic file copy (temp file + rename), like save_stream."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}{TMP_SUFFIX}")
        try:
            shutil.copyfile(source, tmp)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # -- deletes ----------------------------------------------------------------

    def prune_empty_dirs(self, directory: Path, stop_at: Path) -> None:
        """Remove empty folders left after deletes, walking up to the bucket root."""
        try:
            directory, stop_at = directory.resolve(), stop_at.resolve()
            while directory != stop_at and directory.is_relative_to(stop_at):
                directory.rmdir()  # OSError when non-empty — our stop signal
                directory = directory.parent
        except OSError:
            pass

    def delete_single(self, bucket: str, key: str, is_marker: bool) -> None:
        """Delete one object (idempotent, like S3 DeleteObject)."""
        bdir = self.require_bucket(bucket)
        target = self.resolve_safe_path(bucket, key)
        if is_marker:
            if target.is_dir():
                try:
                    target.rmdir()
                    self.prune_empty_dirs(target.parent, bdir)
                except OSError:
                    pass  # non-empty: the "marker" is implicit, nothing to delete
            return
        if target.is_file():
            target.unlink(missing_ok=True)
            meta_file(target).unlink(missing_ok=True)
            self.prune_empty_dirs(target.parent, bdir)

    # -- multipart workspace -----------------------------------------------------

    def manifest_file(self, upload_id: str) -> Path:
        return self.multipart_root / f"{upload_id}.json"

    def part_file(self, upload_id: str, number: int) -> Path:
        return self.multipart_root / f"{upload_id}.{number:05d}.part"

    def cleanup_multipart(self, upload_id: str) -> None:
        for leftover in self.multipart_root.glob(f"{upload_id}.*"):
            leftover.unlink(missing_ok=True)

    # -- statistics ---------------------------------------------------------------

    def storage_usage(self) -> dict:
        """Total object count and bytes under the storage root (skips sidecars)."""
        objects = 0
        total = 0
        for entry in self.storage_root.rglob("*"):
            rel = entry.relative_to(self.storage_root)
            if rel.parts and rel.parts[0].startswith(".fake-s3"):
                continue
            if entry.is_file() and not entry.name.endswith(RESERVED_SUFFIXES):
                objects += 1
                total += entry.stat().st_size
        return {"objects": objects, "bytes": total}
