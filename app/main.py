"""FastAPI surface for the gazetteer web-archive index."""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, File, HTTPException, Path, Query, UploadFile

from .db import Store
from .ingest import (IngestError, VolumeFiles, recheck_after_delete,
                     recheck_pending, replace_volume)
from .warc import WarcFormatError, normalize_record_id

VOLUME_ID = Path(
    description="volume number/label",
    min_length=1,
    max_length=100,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
)

RECORD_ID = Query(
    description=(
        "WARC-Record-ID of the first segment of a segmented response "
        "(angle brackets optional). Any segment's record ID is also accepted."
    ),
    min_length=1,
    max_length=500,
)


def _series_payload(origin_id: str, requested_id: str, info: dict) -> dict:
    series = info["series"]
    rows = info["segments"]
    capture = info["capture"]
    # Each segment carries the status of its series as a whole: a segment is
    # never "verified" on its own, only as part of a complete series.
    verification = series["status"]

    present_numbers = sorted(row["segment_number"] for row in rows)
    max_present = max(present_numbers) if present_numbers else 0
    missing = sorted(set(range(1, max_present + 1)) - set(present_numbers))

    segments = []
    for row in rows:
        segments.append({
            "segment_number": row["segment_number"],
            "present": True,
            "volume": row["volume"],
            "warc_type": row["warc_type"],
            "record_id": row["record_id"],
            "content_length": len(row["content"]),
            "declares_total_length": row["declares_total_length"],
            "verification": verification,
        })
    for number in missing:
        segments.append({
            "segment_number": number,
            "present": False,
            "volume": None,
            "warc_type": None,
            "record_id": None,
            "content_length": None,
            "declares_total_length": None,
            "verification": "missing",
        })
    segments.sort(key=lambda s: s["segment_number"])

    return {
        "record_id": requested_id,
        "origin_id": origin_id,
        "status": series["status"],
        "verified": series["status"] == "verified",
        "reason": series["reason"],
        "detail": series["detail"],
        "problems": json.loads(series["problems_json"] or "[]"),
        "url": series["capture_url"],
        "capture_ts": series["capture_ts"],
        "digest": series["digest"],
        "first_segment_volume": series["first_volume"],
        "http_status": series["http_status"],
        "declared_total_length": series["total_length"],
        "assembled_length": series["assembled_length"],
        "capture": dict(capture) if capture is not None else None,
        "segment_count": len(rows),
        "missing_segments": missing,
        "segments": segments,
    }



def create_app(data_dir: str) -> FastAPI:
    store = Store(os.path.join(data_dir, "index.sqlite3"))
    files = VolumeFiles(os.path.join(data_dir, "volumes"))
    # Finish segmented-series checks interrupted between a committed volume
    # change and its post-commit verification.
    recheck_pending(store)

    app = FastAPI(
        title="Gazetteer Web-Archive Index",
        description=(
            "Indexes uncompressed WARC 1.0/1.1 volumes (response/revisit records "
            "with sha1 payload digests, unencoded HTTP bodies only), reassembles "
            "segmented responses split across volumes, and reports between which "
            "two evidenced captures a URL's body changed."
        ),
        version="2.0.0",
    )
    app.state.store = store
    app.state.files = files

    @app.get("/", include_in_schema=False)
    def root():
        return {"service": "gazetteer-web-archive-index", "docs": "/docs"}

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.put("/volumes/{volume}/warc", summary="Upload or wholly replace a volume")
    async def put_volume(volume: str = VOLUME_ID, file: UploadFile = File(...)):
        """Store an uncompressed WARC (1.0 or 1.1) as *volume*.

        A volume with the same number is replaced wholesale; if the upload
        fails validation or conflicts with other volumes, the old volume is
        kept untouched.  Segmented series touched by the swap are re-verified
        after the commit; only those series change evidence state.
        """
        data = await file.read()
        try:
            return replace_volume(store, files, volume, data, file.filename or "")
        except IngestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/volumes", summary="List stored volumes")
    def list_volumes():
        return {"volumes": [dict(row) for row in store.list_volumes()]}

    @app.delete("/volumes/{volume}", summary="Remove a volume and its captures")
    def delete_volume(volume: str = VOLUME_ID):
        found, affected = store.delete_volume(volume)
        if not found:
            raise HTTPException(status_code=404, detail=f"no such volume: {volume}")
        files.remove(volume)
        return {
            "deleted": volume,
            "segmented_series_rechecked": recheck_after_delete(store, affected),
        }

    def _segments_payload(record_id: str):
        if len(record_id) > 500:
            raise HTTPException(
                status_code=404,
                detail="no segmented response with such a record id")
        try:
            normalized = normalize_record_id(record_id)
        except WarcFormatError:
            raise HTTPException(
                status_code=404,
                detail=f"no segmented response with record id {record_id!r}")
        origin_id = store.resolve_origin_id(normalized)
        if origin_id is None:
            raise HTTPException(
                status_code=404,
                detail=f"no segmented response with record id {record_id!r}")
        info = store.series_info(origin_id)
        if info is None:  # pragma: no cover - resolution guarantees a row
            raise HTTPException(status_code=404,
                                detail=f"no segmented response for {origin_id!r}")
        return _series_payload(origin_id, normalized, info)

    @app.get(
        "/segments",
        summary="Segment table for a segmented response, looked up by first-segment record ID",
        response_description="Segment numbers, volumes and verification status",
    )
    def segments_by_record_id(record_id: str = RECORD_ID):
        """Report every segment's number, volume, and verification status.

        ``record_id`` is the first segment's ``WARC-Record-ID`` (the value a
        continuation points at with ``WARC-Segment-Origin-ID``).  The lookup
        is served from the segment table, so it also works while a
        continuation has arrived before its first segment.
        """
        return _segments_payload(record_id)

    @app.get(
        "/segments/{record_id:path}",
        summary="Segment table (path form; record ID may include angle brackets)",
    )
    def segments_by_path(record_id: str):
        """Same as ``GET /segments?record_id=…`` with the identifier in the path."""
        return _segments_payload(record_id)

    @app.get("/series", summary="List segmented responses and their verification state")
    def list_series():
        """Every segmented series known to the archive, including tombstones."""
        return {"series": store.list_series()}

    @app.get("/changes", summary="Find between which captures a URL's body changed")
    def changes(url: str = Query(description="exact WARC-Target-URI to inspect")):
        """Compare adjacent *evidenced* captures of *url* by capture time.

        A whole-response capture is evidence only when its stored sha1
        payload digest matches the archived body.  A segmented response is
        evidence only when its series is verified as a whole (unique final
        segment, contiguous segment numbers, identical target URIs, matching
        concatenated length, and a reassembled HTTP payload matching the
        first segment's SHA-1).  A revisit is evidence only when a
        digest-verified response with the same digest exists in the volumes
        currently stored.  Returns every pair of neighbouring evidenced
        captures whose bodies differ, plus the captures lacking evidence.
        """
        rows = store.captures_for_url(url)
        captures = [
            {
                "ts": row["ts"],
                "type": row["type"],
                "digest": row["digest"],
                "evidenced": bool(row["evidenced"]),
                "volume": row["volume"],
                "http_status": row["http_status"],
                "segment_origin": row["segment_origin"],
                "segment_status": row["segment_status"],
            }
            for row in rows
        ]
        evidenced = [c for c in captures if c["evidenced"]]
        changes_out = [
            {
                "from_ts": a["ts"],
                "to_ts": b["ts"],
                "from_digest": a["digest"],
                "to_digest": b["digest"],
            }
            for a, b in zip(evidenced, evidenced[1:])
            if a["digest"] != b["digest"]
        ]
        return {
            "url": url,
            "changes": changes_out,
            "unevidenced": [c["ts"] for c in captures if not c["evidenced"]],
            "captures": captures,
        }

    return app


app = create_app(os.environ.get("DATA_DIR", "data"))
