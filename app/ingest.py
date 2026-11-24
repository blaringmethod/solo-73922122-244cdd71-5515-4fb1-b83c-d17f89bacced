"""Ingest pipeline: WARC bytes -> capture/segment rows -> atomic volume swap."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import warc
from .db import Store
from .segments import Segment, verify_series


class IngestError(Exception):
    """An upload failed; the previously stored volume (if any) is kept."""

    status_code = 422

    def __init__(self, detail):
        super().__init__(str(detail))
        self.detail = detail


class CompressedWarcError(IngestError):
    status_code = 400


class NotWarcError(IngestError):
    status_code = 400


class MalformedWarcError(IngestError):
    status_code = 422


class DuplicateCaptureError(IngestError):
    status_code = 409


@dataclass
class Capture:
    url: str
    ts: str
    type: str               # 'response' | 'revisit'
    digest: str             # canonical sha1/base32 payload digest
    valid: int | None       # response: 1/0; revisit: None (evidence derived at query time)
    http_status: int | None
    segment_origin: str | None = None   # set for the capture of a segmented response


@dataclass
class ParsedWarc:
    captures: list[Capture] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _warn_segmented(parsed: ParsedWarc, reason: str, url: str = "",
                    record_id: str = "") -> None:
    """A segmented record we cannot index; the capture stays unevidenced."""
    entry = {"warc_type": "response", "reason": reason, "segmented": True}
    if url:
        entry["url"] = url
    if record_id:
        entry["record_id"] = record_id
    parsed.skipped.append(entry)


def _segment_declared(
        rec: warc.WarcRecord) -> tuple[str, int, int | None] | None:
    """Parse the segmentation fields of a response/continuation record.

    Returns ``(origin_id, segment_number, total_length)`` for a segmented
    record, or ``None`` when a plain response should be handled whole.  A
    continuation without segment number is a hard format error, never a
    ``KeyError`` at call time.
    """
    raw_number = rec.headers.get("warc-segment-number")
    raw_origin = rec.headers.get("warc-segment-origin-id")
    raw_total = rec.headers.get("warc-segment-total-length")
    rtype = rec.headers.get("warc-type", "").strip().lower()
    if raw_number is None and raw_origin is None and raw_total is None \
            and rtype != "continuation":
        return None
    if raw_number is None:
        raise warc.WarcFormatError(
            "segmented/continuation record without WARC-Segment-Number")
    if not raw_number.strip().isdigit() or int(raw_number.strip()) < 1:
        raise warc.WarcFormatError(
            f"malformed WARC-Segment-Number: {raw_number!r}")
    number = int(raw_number.strip())
    total = None
    if raw_total is not None:
        if not raw_total.strip().isdigit():
            raise warc.WarcFormatError(
                f"malformed WARC-Segment-Total-Length: {raw_total!r}")
        total = int(raw_total.strip())
    if rtype == "continuation":
        if not raw_origin:
            raise warc.WarcFormatError(
                "continuation record without WARC-Segment-Origin-ID")
        return warc.normalize_record_id(raw_origin), number, total
    if number != 1:
        raise warc.WarcFormatError(
            f"response record with WARC-Segment-Number {number} (first segment must be 1)")
    if raw_origin:
        raise warc.WarcFormatError(
            "WARC-Segment-Origin-ID is only allowed on continuation records")
    return warc.normalize_record_id(_record_id_of(rec, required=True)), 1, total


def _record_id_of(rec: warc.WarcRecord, required: bool = False) -> str:
    raw = rec.headers.get("warc-record-id")
    if not raw:
        if required:
            raise warc.WarcFormatError(
                "segmented record without WARC-Record-ID")
        return ""
    return warc.normalize_record_id(raw)


def _classify(rec: warc.WarcRecord, parsed: ParsedWarc) -> None:
    rtype = rec.headers.get("warc-type", "").strip().lower()
    if rtype not in ("response", "revisit", "continuation"):
        parsed.skipped.append({
            "warc_type": rtype or "(missing)",
            "reason": "record type is not processed",
        })
        return
    uri = rec.headers.get("warc-target-uri", "").strip()
    if not uri:
        raise warc.WarcFormatError(f"{rtype} record without WARC-Target-URI")
    raw_date = rec.headers.get("warc-date")
    if not raw_date:
        raise warc.WarcFormatError(f"{rtype} record for {uri!r} without WARC-Date")
    ts = warc.parse_warc_date(raw_date)

    if rtype == "continuation":
        if rec.version != "1.1":
            _warn_segmented(
                parsed, "continuation record in a WARC version other than 1.1",
                url=uri, record_id=_record_id_of(rec))
            return
        origin, number, total = _segment_declared(rec)
        parsed.segments.append(Segment(
            origin_id=origin, segment_number=number, url=uri, ts=ts,
            warc_type="continuation", record_id=_record_id_of(rec) or None,
            is_first=False, declares_total_length=total,
            content=rec.content,
        ))
        return

    raw_digest = rec.headers.get("warc-payload-digest")
    if not raw_digest:
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": "no WARC-Payload-Digest",
        })
        return
    digest = warc.normalize_payload_digest(raw_digest)
    if digest is None:
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": "payload digest is not sha1",
        })
        return
    if rtype == "revisit":
        if rec.headers.get("warc-segment-number") is not None:
            _warn_segmented(parsed, "segmented revisit is not indexed", url=uri)
            return
        parsed.captures.append(Capture(uri, ts, "revisit", digest, None, None))
        return

    # WARC-Type: response with a sha1 payload digest.
    ctype = rec.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/http":
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": f"unsupported WARC Content-Type {ctype!r}",
        })
        return

    if rec.version != "1.1" and any(
            rec.headers.get(h) for h in (
                "warc-segment-number", "warc-segment-origin-id",
                "warc-segment-total-length")):
        _warn_segmented(parsed, "segment fields on a WARC record that is not WARC 1.1",
                        url=uri,
                        record_id=_record_id_of(rec) or "")
        return
    declared = _segment_declared(rec) if rec.version == "1.1" else None
    if declared is not None:
        origin, number, total = declared
        segment = Segment(
            origin_id=origin, segment_number=number, url=uri, ts=ts,
            warc_type="response", record_id=_record_id_of(rec) or None,
            is_first=True, declares_total_length=total, content=rec.content,
            digest=digest,
        )
        parsed.segments.append(segment)
        parsed.captures.append(Capture(uri, ts, "response", digest, 0,
                                       None, segment_origin=origin))
        return

    msg = warc.split_http_message(rec.content)
    payload = warc.extract_payload(msg)
    if payload is None:
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": "HTTP body is encoded (only unencoded bodies are indexed)",
        })
        return
    valid = 1 if warc.payload_digest(payload) == digest else 0
    parsed.captures.append(Capture(uri, ts, "response", digest, valid, msg.status))


def parse_warc(data: bytes) -> ParsedWarc:
    """Parse and validate a whole uncompressed WARC file."""
    if data[:2] == b"\x1f\x8b":
        raise CompressedWarcError(
            "gzip-compressed WARC rejected: upload an uncompressed .warc file")
    head = data.lstrip()[:8]
    if head and not head.startswith(b"WARC/"):
        raise NotWarcError("not a WARC file (missing WARC/1.x version line)")
    parsed = ParsedWarc()
    try:
        for rec in warc.iter_warc_records(data):
            _classify(rec, parsed)
    except warc.WarcFormatError as exc:
        raise MalformedWarcError(f"invalid WARC: {exc}") from exc
    seen: dict[tuple[str, str], None] = {}
    for cap in parsed.captures:
        key = (cap.url, cap.ts)
        if key in seen:
            raise DuplicateCaptureError(
                f"duplicate capture inside file: {cap.url!r} at {cap.ts}")
        seen[key] = None
    seen_segments: set[tuple[str, int]] = set()
    for seg in parsed.segments:
        key = (seg.origin_id, seg.segment_number)
        if key in seen_segments:
            raise DuplicateCaptureError(
                "duplicate segment inside file: origin "
                f"{seg.origin_id!r} segment {seg.segment_number}")
        seen_segments.add(key)
    return parsed


class VolumeFiles:
    """Original WARC files kept on disk, one file per volume （原卷留档）."""

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def path(self, volume: str) -> str:
        return os.path.join(self.directory, f"{volume}.warc")

    def stage(self, volume: str, data: bytes) -> str:
        """Write the upload next to the live file and fsync it."""
        os.makedirs(self.directory, exist_ok=True)
        tmp = self.path(volume) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return tmp

    def install(self, volume: str, tmp: str) -> str | None:
        """Move the staged file into place, backing up the current archive.

        Returns the backup path (``None`` when the volume had no file).  The
        backup lets a failed database swap restore the old archive byte for
        byte.
        """
        live = self.path(volume)
        backup = live + ".prev"
        had_old = os.path.exists(live)
        if had_old:
            os.replace(live, backup)
        try:
            os.replace(tmp, live)
        except BaseException:
            if had_old:
                os.replace(backup, live)
            raise
        return backup if had_old else None

    def restore(self, volume: str, backup: str | None) -> None:
        """Put the previous archive back after a failed swap."""
        live = self.path(volume)
        try:
            os.unlink(live)
        except FileNotFoundError:
            pass
        if backup is not None:
            os.replace(backup, live)

    def finish(self, backup: str | None) -> None:
        if backup is not None:
            try:
                os.unlink(backup)
            except FileNotFoundError:
                pass

    def remove(self, volume: str) -> None:
        try:
            os.unlink(self.path(volume))
        except FileNotFoundError:
            pass


def recheck_pending(store: Store) -> list[dict]:
    """Verify segmented series left ``pending`` by an interrupted write.

    A volume swap/delete commits the new segment set together with a
    ``pending`` marker, then verifies affected series outside the write
    transaction.  If the process stops in between, startup re-runs exactly
    those verifications; no unrelated series is rescanned.
    """
    return _verify_series(store, store.pending_origins())


def _verify_series(store: Store, origin_ids: list[str]) -> list[dict]:
    """Re-verify the given segmented series after a write has committed.

    Each series is snapshotted, reassembled and checked in isolation.  A
    series whose revision changed since its snapshot (because a concurrent
    upload or delete touched it) is left ``pending`` for that write's own
    verification pass; the result that lost the race is discarded.

    No write transaction is open here, so uploads of unrelated volumes can
    commit while a long concatenation/hash check is running.
    """
    if not origin_ids:
        return []
    outcomes = []
    for origin_id in origin_ids:
        if not origin_id:
            continue
        snapshot = store.snapshot_series(origin_id)
        if snapshot is None:
            continue
        segments = [
            Segment(
                origin_id=row["origin_id"],
                segment_number=row["segment_number"],
                url=row["url"], ts=row["ts"], warc_type=row["warc_type"],
                record_id=row["record_id"], is_first=row["warc_type"] == "response",
                declares_total_length=row["declares_total_length"],
                content=bytes(row["content"]),
                digest=row["payload_digest"],
                http_status=row["http_status"],
                volume=row["volume"], id=row["id"],
            )
            for row in snapshot.segments
        ]
        result = verify_series(origin_id, segments)
        applied = store.apply_verification(origin_id, snapshot.revision, result)
        outcomes.append({
            "origin_id": origin_id,
            "status": result.status,
            "reason": result.reason,
            "applied": applied,
        })
    return outcomes


def replace_volume(store: Store, files: VolumeFiles, volume: str,
                   data: bytes, filename: str) -> dict:
    """Validate the upload, then swap the volume atomically.

    Any failure before or during the database transaction leaves the old
    volume (index rows and archived file) untouched.  Segmented series
    touched by the swap are re-verified after the commit; their evidence
    state changes together with the volume.
    """
    parsed = parse_warc(data)
    tmp = files.stage(volume, data)
    volume_row = {
        "filename": filename,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "records": len(parsed.captures),
        "segments": len(parsed.segments),
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Install the archive first, keeping the old bytes as a backup, so a
    # failed/conflicting database swap can put the old volume back exactly.
    backup = files.install(volume, tmp)
    try:
        swap = store.swap_volume(volume, parsed.captures, parsed.segments,
                                 volume_row)
    except BaseException:
        files.restore(volume, backup)
        raise
    if swap.conflicts:
        files.restore(volume, backup)
        raise DuplicateCaptureError(
            {"detail": "captures already exist in other volumes",
             "conflicts": swap.conflicts})
    files.finish(backup)
    # Verification runs after the write transaction has committed.  A failure
    # here leaves the swap in place with its series marked pending (startup
    # and later writes retry it); it never reports a failed write for a
    # committed volume.
    try:
        verification = _verify_series(store, swap.affected_origins)
    except Exception as exc:  # pragma: no cover - defensive
        verification = [{"error": f"recheck deferred: {exc}"}]
    responses = sum(1 for c in parsed.captures if c.type == "response")
    whole = [c for c in parsed.captures
             if c.type == "response" and c.segment_origin is None]
    return {
        "volume": volume,
        "stored_file": files.path(volume),
        "records_indexed": len(parsed.captures),
        "responses": responses,
        "responses_verified": sum(1 for c in whole if c.valid == 1),
        "responses_digest_failed": sum(1 for c in whole if c.valid == 0),
        "segmented_responses": sum(1 for c in parsed.captures
                                   if c.segment_origin is not None),
        "revisits": len(parsed.captures) - responses,
        "segments": len(parsed.segments),
        "segmented_series_rechecked": verification,
        "records_skipped": len(parsed.skipped),
        "skipped": parsed.skipped,
    }


def recheck_after_delete(store: Store, affected_origins: list[str]) -> list[dict]:
    """Re-verify segmented series touched by a volume delete."""
    return _verify_series(store, affected_origins)
