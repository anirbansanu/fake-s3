"""XML response helpers and S3 time formats."""

from datetime import datetime, timezone
from email.utils import formatdate
from xml.etree import ElementTree as ET

from fastapi import Response

from ..core.errors import S3Error

XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"


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
