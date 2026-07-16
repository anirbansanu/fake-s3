"""aws-chunked request-body decoding."""

from typing import AsyncIterator

from fastapi import Request

from ..core.errors import S3Error


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
