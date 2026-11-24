"""Segmented WARC response reassembly and verification.

A logical response may be split into a first ``response`` record carrying
``WARC-Segment-Number: 1`` and any number of ``continuation`` records.  The
content blocks are concatenated in segment-number order and the result is
the complete archived HTTP response (status line, headers and body).

A segmented capture is *evidence* only when the series is complete and
self-consistent:

* exactly one first segment and exactly one final segment,
* segment numbers run from 1 to the final number with no gaps or duplicates,
* every segment names the same target URI,
* the concatenated content length equals the final segment's
  ``WARC-Segment-Total-Length``,
* the reassembled HTTP payload matches the first segment's
  ``WARC-Payload-Digest`` (sha1).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import warc

VERIFIED = "verified"
INCOMPLETE = "incomplete"
INVALID = "invalid"
ABSENT = "absent"


@dataclass
class Segment:
    origin_id: str
    segment_number: int
    url: str
    ts: str
    warc_type: str                 # 'response' (first) or 'continuation'
    record_id: str | None
    is_first: bool
    declares_total_length: int | None
    content: bytes
    digest: str | None = None      # first segment's logical payload digest
    http_status: int | None = None
    volume: str | None = None
    id: int | None = None


@dataclass
class Problem:
    code: str
    detail: str
    segment_number: int | None = None

    def as_dict(self) -> dict:
        out = {"code": self.code, "detail": self.detail}
        if self.segment_number is not None:
            out["segment_number"] = self.segment_number
        return out


@dataclass
class SeriesResult:
    origin_id: str
    status: str
    reason: str | None = None
    detail: str | None = None
    problems: list[Problem] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    first: Segment | None = None
    capture_url: str | None = None
    capture_ts: str | None = None
    digest: str | None = None
    first_volume: str | None = None
    http_status: int | None = None
    total_length: int | None = None
    assembled_length: int | None = None
    missing_segment_numbers: list[int] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED


def _fail(result: SeriesResult, status: str, code: str, detail: str,
          segment_number: int | None = None) -> SeriesResult:
    result.status = status
    if result.reason is None:
        result.reason = code
        result.detail = detail
    result.problems.append(Problem(code, detail, segment_number))
    return result


def verify_series(origin_id: str, segments: list[Segment]) -> SeriesResult:
    """Verify one segmented series from its current segment rows.

    *segments* may contain duplicate segment numbers and records from any
    number of volumes; all of that is part of the evidence decision.
    """
    result = SeriesResult(origin_id=origin_id, status=ABSENT,
                          segments=sorted(segments, key=lambda s: (
                              s.segment_number, s.volume or "", s.id or 0)))

    usable = [s for s in segments if s.warc_type in ("response", "continuation")]
    if not usable:
        return result

    firsts = [s for s in usable
              if s.warc_type == "response" and s.segment_number == 1]
    if firsts:
        result.first = firsts[0]
        result.capture_url = firsts[0].url
        result.capture_ts = firsts[0].ts
        result.digest = firsts[0].digest
        result.first_volume = firsts[0].volume

    numbers = [s.segment_number for s in usable]
    counts: dict[int, int] = {}
    for number in numbers:
        counts[number] = counts.get(number, 0) + 1

    max_number = max(numbers)
    result.missing_segment_numbers = sorted(
        n for n in range(1, max_number + 1) if n not in counts)

    if any(s.warc_type == "continuation" and s.segment_number == 1 for s in usable):
        return _fail(result, INVALID, "continuation_cannot_be_first",
                     "a continuation record may not carry segment number 1", 1)
    if any(s.warc_type == "response" and s.segment_number > 1 for s in usable):
        return _fail(result, INVALID, "unexpected_response_segment",
                     "a response record other than the first segment must be a continuation")
    if len(firsts) > 1:
        return _fail(result, INVALID, "multiple_first_segments",
                     "more than one first segment claims segment number 1", 1)

    duplicates = sorted(n for n, count in counts.items() if count > 1)
    if duplicates:
        return _fail(result, INVALID, "duplicate_segment_number",
                     f"segment number(s) {duplicates} occur more than once",
                     duplicates[0])

    if not firsts:
        return _fail(result, INCOMPLETE, "missing_first_segment",
                     "no response record with segment number 1 has been ingested")

    if result.missing_segment_numbers:
        return _fail(result, INCOMPLETE, "segment_gap",
                     "segment numbers are not contiguous from 1 to "
                     f"{max_number}: missing {result.missing_segment_numbers}",
                     result.missing_segment_numbers[0])

    urls = {s.url for s in usable}
    if len(urls) > 1:
        bad = next(s for s in usable if s.url != firsts[0].url)
        return _fail(result, INVALID, "target_uri_mismatch",
                     "segments name different target URIs", bad.segment_number)

    declared = [s for s in usable if s.declares_total_length is not None]
    if not declared:
        return _fail(result, INCOMPLETE, "missing_last_segment",
                     "no segment declares WARC-Segment-Total-Length")
    if len(declared) > 1:
        return _fail(result, INVALID, "multiple_last_segments",
                     "more than one segment declares WARC-Segment-Total-Length",
                     sorted(s.segment_number for s in declared)[1])
    last = declared[0]
    result.total_length = last.declares_total_length
    if last.segment_number != max_number:
        return _fail(result, INVALID, "last_segment_not_final",
                     f"segment {last.segment_number} declares the total length "
                     f"but segment {max_number} is not followed by a final segment",
                     last.segment_number)
    if last.warc_type != "continuation":
        return _fail(result, INVALID, "last_segment_not_continuation",
                     "WARC-Segment-Total-Length must be carried by a continuation",
                     last.segment_number)
    if firsts[0].digest is None:
        return _fail(result, INVALID, "missing_payload_digest",
                     "the first segment has no sha1 WARC-Payload-Digest", 1)

    assembled = b"".join(s.content for s in sorted(
        usable, key=lambda s: s.segment_number))
    result.assembled_length = len(assembled)
    if len(assembled) != last.declares_total_length:
        return _fail(result, INVALID, "segment_total_length_mismatch",
                     "reassembled content is "
                     f"{len(assembled)} bytes but the final segment declares "
                     f"{last.declares_total_length}", last.segment_number)

    try:
        msg = warc.split_http_message(assembled)
    except warc.WarcFormatError as exc:
        return _fail(result, INVALID, "malformed_http_response", str(exc))
    result.http_status = msg.status

    payload = warc.extract_payload(msg)
    if payload is None:
        return _fail(result, INVALID, "http_body_encoded",
                     "the reassembled HTTP body is encoded and out of scope")

    actual = warc.payload_digest(payload)
    if actual != firsts[0].digest:
        return _fail(result, INVALID, "payload_digest_mismatch",
                     "reassembled HTTP payload does not match the first "
                     "segment's SHA-1 payload digest", 1)

    result.status = VERIFIED
    return result


__all__ = [
    "Segment", "SeriesResult", "Problem", "verify_series",
    "VERIFIED", "INCOMPLETE", "INVALID", "ABSENT",
]
