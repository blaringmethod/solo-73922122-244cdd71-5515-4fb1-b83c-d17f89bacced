"""Shared WARC-building helpers for the test-suite."""
from __future__ import annotations

import base64
import hashlib
import uuid


def b32(data: bytes) -> str:
    return base64.b32encode(hashlib.sha1(data).digest()).decode().rstrip("=")


def http_response(body: bytes, extra_headers: tuple[str, ...] = (),
                  status: str = "200 OK") -> bytes:
    lines = [f"HTTP/1.1 {status}", "Content-Type: text/html; charset=utf-8",
             *extra_headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def chunked(body: bytes, sizes=(5,)) -> bytes:
    """Encode body as HTTP chunked, splitting at the given chunk sizes."""
    out, pos = b"", 0
    chunks = []
    for size in sizes:
        chunks.append(body[pos:pos + size])
        pos += size
    if pos < len(body):
        chunks.append(body[pos:])
    for chunk in chunks:
        out += f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
    return out + b"0\r\n\r\n"


def record(rtype: str, url: str, date: str, content: bytes = b"",
           digest: str | None = None,
           content_type: str = "application/http; msgtype=response",
           extra_headers: tuple[str, ...] = (),
           version: str = "1.0",
           record_id: str | None = None) -> bytes:
    hdrs = [
        f"WARC/{version}",
        f"WARC-Type: {rtype}",
        f"WARC-Target-URI: {url}",
        f"WARC-Date: {date}",
        f"WARC-Record-ID: <{record_id or ('urn:uuid:' + str(uuid.uuid4()))}>",
        f"Content-Type: {content_type}",
    ]
    if digest is not None:
        hdrs.append(f"WARC-Payload-Digest: sha1:{digest}")
    hdrs.extend(extra_headers)
    head = ("\r\n".join(hdrs) + "\r\n").encode()
    return head + f"Content-Length: {len(content)}\r\n\r\n".encode() + content + b"\r\n\r\n"


def response_record(url: str, date: str, body: bytes,
                    http_headers: tuple[str, ...] = (),
                    digest: str | None = None) -> bytes:
    http = http_response(body, http_headers)
    return record("response", url, date, http,
                  digest=b32(body) if digest is None else digest)


def revisit_record(url: str, date: str, digest: str) -> bytes:
    http = ("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n").encode()
    return record("revisit", url, date, http, digest=digest,
                  content_type="application/http; msgtype=response")


def warcinfo_record() -> bytes:
    return record("warcinfo", "", "2024-01-01T00:00:00Z",
                  b"software: gazetteer-test\r\n",
                  content_type="application/warc-fields")


def first_segment(url: str, date: str, content: bytes, digest: str,
                  record_id: str, total_length: int | None = None,
                  http_status: str = "200 OK") -> bytes:
    """First segment of a WARC 1.1 segmented response (WARC-Segment-Number 1).

    WARC 1.1 puts ``WARC-Segment-Total-Length`` on the final continuation,
    so *total_length* is accepted for call-site symmetry but not emitted.
    """
    return record("response", url, date, content, digest=digest,
                  version="1.1", record_id=record_id,
                  extra_headers=("WARC-Segment-Number: 1",))


def continuation(url: str, date: str, content: bytes, origin_id: str,
                 segment_number: int, total_length: int | None = None,
                 digest: str | None = None,
                 record_id: str | None = None) -> bytes:
    """A WARC 1.1 continuation record for an already-started series."""
    extra = [f"WARC-Segment-Origin-ID: <{origin_id}>",
             f"WARC-Segment-Number: {segment_number}"]
    if total_length is not None:
        extra.append(f"WARC-Segment-Total-Length: {total_length}")
    return record("continuation", url, date, content, digest=digest,
                  version="1.1", record_id=record_id,
                  extra_headers=tuple(extra))


def split_http(body: bytes, cut: int) -> tuple[bytes, bytes]:
    """Split an archived HTTP response at *cut* bytes of its content block."""
    http = http_response(body)
    return http[:cut], http[cut:]
