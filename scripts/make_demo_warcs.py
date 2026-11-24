#!/usr/bin/env python3
"""Generate the demo WARC volumes used by scripts/demo_flow.sh.

Story: the gazetteer office crawls its collection bulletin
(馆藏公告) four times a year.  Bodies V1 -> V2 -> V2 (revisit) -> V3,
then one corrupt capture, then a revisit of V1.

  vol1.warc   t1 response V1, t2 response V2            (kept, then replaced)
  vol2.warc   t3 revisit V2, t4 response V3,
              t5 response V3 with a WRONG digest, t6 revisit V1
  vol1b.warc  t1 response V1 only  -> replacing vol1 with this removes the
              last verified V2 body, so vol2's t3 revisit loses evidence
  conflict.warc  one response at t4 -> rejected (instant already held by vol2)

A second bulletin is split across WARC volumes (WARC 1.1 segmentation):

  seg-head.warc     s1  first segment of the response (segment 1)
  seg-tail.warc     s1  final continuation (segment 2, WARC-Segment-Total-Length)
  seg-witness.warc  s2  revisit of the same payload, evidencing it in another
                        volume; loses evidence while the series is incomplete
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import uuid

URL = "http://gazetteer.example.gov.cn/zcgg/notice.html"
V1 = "馆藏公告（第一版）：本馆藏有民国县志三十种、清代府志八种。".encode()
V2 = "馆藏公告（第二版）：新增解放后第一轮修志成果十二种。".encode()
V3 = "馆藏公告（第三版）：数字方志馆上线，支持全文检索。".encode()

T1 = "2024-03-01T09:00:00Z"
T2 = "2024-06-01T09:00:00Z"
T3 = "2024-09-01T09:00:00Z"
T4 = "2024-12-01T09:00:00Z"
T5 = "2025-03-01T09:00:00Z"
T6 = "2025-06-01T09:00:00Z"

SEG_URL = "http://gazetteer.example.gov.cn/zcgg/segmented-notice.html"
SEG_BODY = "馆藏公告（分段版）：本馆藏志书目录已按版本分卷保存，供馆员核对。".encode()
SEG_T1 = "2025-09-01T09:00:00Z"
SEG_T2 = "2025-12-01T09:00:00Z"
SEG_HEAD_ID = "urn:uuid:5e6f7a8b-0001-4000-8000-000000000001"
SEG_SPLIT = 60


def b32(data: bytes) -> str:
    return base64.b32encode(hashlib.sha1(data).digest()).decode().rstrip("=")


def record(rtype, url, date, content=b"", digest=None,
           content_type="application/http; msgtype=response", extra=(),
           version="1.0", record_id=None):
    hdrs = [f"WARC/{version}", f"WARC-Type: {rtype}",
            f"WARC-Target-URI: {url}", f"WARC-Date: {date}",
            f"WARC-Record-ID: <{record_id or 'urn:uuid:' + str(uuid.uuid4())}>"]
    hdrs.append(f"Content-Type: {content_type}")
    if digest is not None:
        hdrs.append(f"WARC-Payload-Digest: sha1:{digest}")
    hdrs.extend(extra)
    head = ("\r\n".join(hdrs) + "\r\n").encode()
    return head + b"Content-Length: " + str(len(content)).encode() + b"\r\n\r\n" + content + b"\r\n\r\n"


def response(url, date, body, digest=None):
    http = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + body
    return record("response", url, date, http, digest=b32(body) if digest is None else digest)


def revisit(url, date, body_digest):
    http = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    return record("revisit", url, date, http, digest=body_digest)


def segment_records():
    """Return (head, tail, witness) WARC bytes for the segmented bulletin."""
    http = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            + SEG_BODY)
    head, tail = http[:SEG_SPLIT], http[SEG_SPLIT:]
    total = len(http)
    head_rec = record(
        "response", SEG_URL, SEG_T1, head, digest=b32(SEG_BODY),
        extra=["WARC-Segment-Number: 1"],
        version="1.1", record_id=SEG_HEAD_ID)
    tail_rec = record(
        "continuation", SEG_URL, SEG_T1, tail,
        extra=[f"WARC-Segment-Origin-ID: <{SEG_HEAD_ID}>",
               "WARC-Segment-Number: 2",
               f"WARC-Segment-Total-Length: {total}"],
        version="1.1")
    witness = revisit(SEG_URL, SEG_T2, b32(SEG_BODY))
    return head_rec, tail_rec, witness


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)

    warcinfo = record("warcinfo", "", T1, b"software: demo-crawler 1.0\r\n",
                      content_type="application/warc-fields")
    request = record("request", URL, T1,
                     b"GET /zcgg/notice.html HTTP/1.1\r\nHost: gazetteer.example.gov.cn\r\n\r\n",
                     content_type="application/http; msgtype=request")

    vol1 = warcinfo + response(URL, T1, V1) + response(URL, T2, V2) + request
    vol2 = (revisit(URL, T3, b32(V2)) + response(URL, T4, V3)
            + response(URL, T5, V3, digest=b32(b"corrupted-digest"))
            + revisit(URL, T6, b32(V1)))
    vol1b = warcinfo + response(URL, T1, V1)
    conflict = response(URL, T4, V1)

    seg_head, seg_tail, seg_witness = segment_records()

    for name, data in [("vol1.warc", vol1), ("vol2.warc", vol2),
                       ("vol1b.warc", vol1b), ("conflict.warc", conflict),
                       ("seg-head.warc", seg_head), ("seg-tail.warc", seg_tail),
                       ("seg-witness.warc", seg_witness)]:
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"wrote {path} ({len(data)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo")
