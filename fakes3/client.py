"""
Minimal S3 + admin HTTP client for fakes3, built on the standard library.

Used by the CLI (and available to scripts) so the packaged executable does
not need to bundle boto3. fakes3 never verifies signatures, so plain HTTP
requests are sufficient.
"""

import json
import shutil
from pathlib import Path
from typing import BinaryIO
from urllib import error, request
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if _local(child.tag) == name:
            return child.text or ""
    return ""


class ClientError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"{code} ({status}): {message}")


class FakeS3Client:
    def __init__(self, endpoint: str = "http://localhost:9000", timeout: float = 60.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------------

    def _url(self, path: str, query: dict | None = None) -> str:
        url = self.endpoint + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        return url

    def _request(self, method: str, path: str, query: dict | None = None,
                 data: bytes | BinaryIO | None = None,
                 headers: dict | None = None):
        req = request.Request(self._url(path, query), data=data, method=method)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            return request.urlopen(req, timeout=self.timeout)
        except error.HTTPError as exc:
            body = exc.read()
            code, message = str(exc.code), exc.reason or ""
            if body.startswith(b"<?xml") or body.startswith(b"<Error"):
                try:
                    root = ET.fromstring(body)
                    code = _find(root, "Code") or code
                    message = _find(root, "Message") or message
                except ET.ParseError:
                    pass
            raise ClientError(exc.code, code, message) from None
        except error.URLError as exc:
            raise ClientError(0, "ConnectionError",
                              f"Cannot reach {self.endpoint}: {exc.reason}") from None

    def _object_path(self, bucket: str, key: str) -> str:
        return f"/{quote(bucket)}/{quote(key, safe='/')}"

    # -- admin API -----------------------------------------------------------------

    def _admin(self, path: str, query: dict | None = None) -> dict:
        with self._request("GET", f"/_fakes3/{path}", query) as resp:
            return json.loads(resp.read())

    def status(self) -> dict:
        return self._admin("status")

    def stats(self) -> dict:
        return self._admin("stats")

    def logs(self, limit: int = 200, level: str | None = None) -> list[dict]:
        return self._admin("logs", {"limit": limit, "level": level})["logs"]

    def server_config(self) -> dict:
        return self._admin("config")

    def is_reachable(self) -> bool:
        try:
            self.status()
            return True
        except ClientError:
            return False

    # -- buckets ---------------------------------------------------------------------

    def list_buckets(self) -> list[dict]:
        with self._request("GET", "/") as resp:
            root = ET.fromstring(resp.read())
        buckets = []
        for el in root.iter():
            if _local(el.tag) == "Bucket":
                buckets.append({"name": _find(el, "Name"),
                                "created": _find(el, "CreationDate")})
        return buckets

    def create_bucket(self, bucket: str) -> None:
        self._request("PUT", f"/{quote(bucket)}").close()

    def delete_bucket(self, bucket: str, force: bool = False) -> None:
        if force:
            for obj in self.list_objects(bucket)["objects"]:
                self.delete_object(bucket, obj["key"])
        self._request("DELETE", f"/{quote(bucket)}").close()

    def bucket_exists(self, bucket: str) -> bool:
        try:
            self._request("HEAD", f"/{quote(bucket)}").close()
            return True
        except ClientError as exc:
            if exc.status == 404:
                return False
            raise

    # -- objects -----------------------------------------------------------------------

    def list_objects(self, bucket: str, prefix: str | None = None,
                     delimiter: str | None = None) -> dict:
        """All objects (paginated internally) + common prefixes."""
        objects: list[dict] = []
        prefixes: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "delimiter": delimiter,
                     "continuation-token": token}
            with self._request("GET", f"/{quote(bucket)}", query) as resp:
                root = ET.fromstring(resp.read())
            for el in root:
                name = _local(el.tag)
                if name == "Contents":
                    objects.append({
                        "key": _find(el, "Key"),
                        "size": int(_find(el, "Size") or 0),
                        "last_modified": _find(el, "LastModified"),
                        "etag": _find(el, "ETag").strip('"'),
                    })
                elif name == "CommonPrefixes":
                    prefixes.append(_find(el, "Prefix"))
            token = _find(root, "NextContinuationToken") or None
            if not token:
                break
        return {"objects": objects, "prefixes": prefixes}

    def put_object(self, bucket: str, key: str, source: Path | bytes,
                   content_type: str | None = None,
                   metadata: dict | None = None) -> str:
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        for name, value in (metadata or {}).items():
            headers[f"x-amz-meta-{name}"] = value
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        else:
            data = source
        with self._request("PUT", self._object_path(bucket, key),
                           data=data, headers=headers) as resp:
            return (resp.headers.get("ETag") or "").strip('"')

    def get_object(self, bucket: str, key: str, dest: Path | None = None) -> bytes | Path:
        with self._request("GET", self._object_path(bucket, key)) as resp:
            if dest is None:
                return resp.read()
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                shutil.copyfileobj(resp, out)
            return dest

    def head_object(self, bucket: str, key: str) -> dict:
        with self._request("HEAD", self._object_path(bucket, key)) as resp:
            headers = resp.headers
            meta = {
                name[len("x-amz-meta-"):]: value
                for name, value in headers.items()
                if name.lower().startswith("x-amz-meta-")
            }
            return {
                "key": key,
                "size": int(headers.get("Content-Length") or 0),
                "etag": (headers.get("ETag") or "").strip('"'),
                "content_type": headers.get("Content-Type"),
                "last_modified": headers.get("Last-Modified"),
                "metadata": meta,
            }

    def delete_object(self, bucket: str, key: str) -> None:
        self._request("DELETE", self._object_path(bucket, key)).close()

    def copy_object(self, src_bucket: str, src_key: str,
                    dst_bucket: str, dst_key: str) -> None:
        headers = {"x-amz-copy-source": f"/{quote(src_bucket)}/{quote(src_key, safe='/')}"}
        self._request("PUT", self._object_path(dst_bucket, dst_key),
                      headers=headers).close()

    def move_object(self, src_bucket: str, src_key: str,
                    dst_bucket: str, dst_key: str) -> None:
        """S3 has no rename/move: copy then delete, like every S3 tool."""
        self.copy_object(src_bucket, src_key, dst_bucket, dst_key)
        self.delete_object(src_bucket, src_key)
