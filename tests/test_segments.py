"""End-to-end tests for segmented responses split across WARC volumes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.ingest import VolumeFiles, recheck_pending, replace_volume
from app.main import create_app
from app.segments import Segment, verify_series
from tests.warcbuild import (b32, continuation, first_segment, http_response,
                             response_record, revisit_record, split_http)

URL = "http://gazetteer.example.cn/notice.html"
OTHER = "http://gazetteer.example.cn/other.html"
V1 = "馆藏公告 第一版：本馆藏有民国县志三十种。".encode()
V2 = "馆藏公告 第二版：新增解放后府志十二种。".encode()
FIRST_ID = "urn:uuid:11111111-1111-4111-8111-111111111111"
SECOND_ID = "urn:uuid:22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    with TestClient(app) as test_client:
        test_client.data_dir = tmp_path / "data"
        yield test_client


def put(client, volume, payload: bytes, filename="vol.warc"):
    return client.put(f"/volumes/{volume}/warc",
                      files={"file": (filename, payload, "application/warc")})


def segments(client, record_id=FIRST_ID):
    return client.get("/segments", params={"record_id": record_id})


def changes(client, url=URL):
    resp = client.get("/changes", params={"url": url})
    assert resp.status_code == 200
    return resp.json()


def series_warc(body=V2, cut=None, total=None, digest=None):
    """A first segment + one final continuation, split inside the HTTP block."""
    head, tail = split_http(body, cut if cut is not None
                            else len(http_response(body)) // 2)
    full = http_response(body)
    total = len(full) if total is None else total
    return (first_segment(URL, "2024-03-01T09:00:00Z", head,
                          digest or b32(body), FIRST_ID, total_length=total)
            + continuation(URL, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                           total_length=total))


def test_segmented_capture_verified_and_reassembled(client):
    resp = put(client, "vol1", series_warc())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["segments"] == 2
    assert body["segmented_responses"] == 1
    out = segments(client)
    assert out.status_code == 200, out.text
    info = out.json()
    assert info["status"] == "verified"
    assert info["verified"] is True
    assert info["reason"] is None
    assert info["assembled_length"] == info["declared_total_length"]
    assert [s["segment_number"] for s in info["segments"]] == [1, 2]
    assert [s["volume"] for s in info["segments"]] == ["vol1", "vol1"]
    assert [s["verification"] for s in info["segments"]] == ["verified", "verified"]
    assert changes(client)["unevidenced"] == []


def test_continuation_before_first_segment_then_first_completes(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    cont = continuation(URL, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                        total_length=total)
    assert put(client, "vol1", cont).status_code == 200
    info = segments(client).json()
    assert info["status"] == "incomplete"
    assert info["missing_segments"] == [1]
    assert [s["segment_number"] for s in info["segments"]] == [1, 2]
    assert [s["present"] for s in info["segments"]] == [False, True]
    assert changes(client)["unevidenced"] == []

    first = first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2),
                          FIRST_ID, total_length=total)
    assert put(client, "vol2", first).status_code == 200
    info = segments(client).json()
    assert info["status"] == "verified"
    assert [s["volume"] for s in info["segments"]] == ["vol2", "vol1"]
    assert changes(client)["unevidenced"] == []


def test_lookup_by_any_segment_record_id(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    warc = (first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID)
            + continuation(URL, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                           total_length=total, record_id=SECOND_ID))
    put(client, "vol1", warc)
    for record_id in (FIRST_ID, f"<{FIRST_ID}>", SECOND_ID):
        info = segments(client, record_id).json()
        assert info["origin_id"] == FIRST_ID
        assert info["status"] == "verified"
    assert segments(client, "urn:uuid:nope").status_code == 404
    path = client.get(f"/segments/{FIRST_ID}")
    assert path.status_code == 200 and path.json()["status"] == "verified"


def test_missing_final_segment_is_incomplete(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    put(client, "vol1", first_segment(URL, "2024-03-01T09:00:00Z", head,
                                      b32(V2), FIRST_ID, total_length=total))
    info = segments(client).json()
    assert info["status"] == "incomplete"
    assert info["reason"] == "missing_last_segment"
    assert changes(client)["unevidenced"] == ["2024-03-01T09:00:00Z"]
    # adding the final segment restores evidence
    put(client, "vol2", continuation(URL, "2024-03-01T09:00:00Z", tail,
                                     FIRST_ID, 2, total_length=total))
    assert segments(client).json()["status"] == "verified"
    assert changes(client)["unevidenced"] == []


def test_gap_in_segment_numbers_is_incomplete(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    warc = (first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID,
                          total_length=total)
            + continuation(URL, "2024-03-01T09:00:00Z", b"", FIRST_ID, 3,
                           total_length=total))
    put(client, "vol1", warc)
    info = segments(client).json()
    assert info["status"] == "incomplete"
    assert info["reason"] == "segment_gap"
    assert info["missing_segments"] == [2]
    assert len(info["segments"]) == 3
    assert info["segments"][1]["present"] is False


def test_duplicate_segment_number_across_volumes_invalid(client):
    head, _ = split_http(V2, 20)
    total = len(http_response(V2))
    first = first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID,
                          total_length=total)
    second = continuation(URL, "2024-03-01T09:00:00Z", b"x", FIRST_ID, 2,
                          total_length=total)
    assert put(client, "vol1", first + second).status_code == 200
    resp = put(client, "vol2", continuation(URL, "2024-03-01T09:00:00Z", b"y",
                                            FIRST_ID, 2, total_length=total))
    assert resp.status_code == 200
    info = segments(client).json()
    assert info["status"] == "invalid"
    assert info["reason"] == "duplicate_segment_number"
    assert changes(client)["unevidenced"] == ["2024-03-01T09:00:00Z"]


def test_multiple_final_segments_invalid(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    warc = (first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID,
                          total_length=total)
            + continuation(URL, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                           total_length=total)
            + continuation(URL, "2024-03-01T09:00:00Z", b"", FIRST_ID, 3,
                           total_length=total))
    put(client, "vol1", warc)
    info = segments(client).json()
    assert info["status"] == "invalid"
    assert info["reason"] == "multiple_last_segments"
    assert changes(client)["unevidenced"] == ["2024-03-01T09:00:00Z"]


def test_segment_urls_must_match(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    warc = (first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID,
                          total_length=total)
            + continuation(OTHER, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                           total_length=total))
    put(client, "vol1", warc)
    info = segments(client).json()
    assert info["status"] == "invalid"
    assert info["reason"] == "target_uri_mismatch"


def test_concatenated_length_must_match_final_declaration(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    for bad_total, reason in ((total + 5, "segment_total_length_mismatch"),
                              (None, "missing_last_segment")):
        put(client, "vol1", (first_segment(
            URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID,
            total_length=total)
            + continuation(URL, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                           total_length=bad_total)))
        info = segments(client).json()
        assert info["status"] == "invalid" if bad_total else \
            info["status"] == "incomplete"
        assert info["reason"] == reason


def test_payload_digest_of_first_segment_must_verify(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    put(client, "vol1", (first_segment(URL, "2024-03-01T09:00:00Z", head,
                                       b32(b"forged"), FIRST_ID,
                                       total_length=total)
                         + continuation(URL, "2024-03-01T09:00:00Z", tail,
                                        FIRST_ID, 2, total_length=total)))
    info = segments(client).json()
    assert info["status"] == "invalid"
    assert info["reason"] == "payload_digest_mismatch"
    assert changes(client)["unevidenced"] == ["2024-03-01T09:00:00Z"]


def test_wholly_replaced_volume_reverifies_only_its_series(client):
    # series A verified from vol1 (first) + vol2 (continuation)
    head_a, tail_a = split_http(V2, 20)
    total_a = len(http_response(V2))
    put(client, "vol1", first_segment(URL, "2024-03-01T09:00:00Z", head_a,
                                      b32(V2), FIRST_ID, total_length=total_a))
    put(client, "vol2", continuation(URL, "2024-03-01T09:00:00Z", tail_a,
                                     FIRST_ID, 2, total_length=total_a))
    # unrelated whole response in vol3
    put(client, "vol3", response_record(URL, "2024-06-01T09:00:00Z", V1))
    assert segments(client).json()["status"] == "verified"
    # replace vol3; the segmented series must stay verified without a rescan
    resp = put(client, "vol3", response_record(URL, "2024-06-01T09:00:00Z", V1))
    assert resp.status_code == 200
    assert [s["origin_id"] for s in resp.json()["segmented_series_rechecked"]] == []
    assert segments(client).json()["status"] == "verified"


def test_swapping_a_segment_volume_rechecks_the_series(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    put(client, "vol1", first_segment(URL, "2024-03-01T09:00:00Z", head,
                                      b32(V2), FIRST_ID, total_length=total))
    put(client, "vol2", continuation(URL, "2024-03-01T09:00:00Z", tail,
                                     FIRST_ID, 2, total_length=total))
    assert segments(client).json()["status"] == "verified"
    # replace the continuation volume with an empty WARC
    resp = put(client, "vol2", b"WARC/1.1\r\nWARC-Type: warcinfo\r\n"
                               b"WARC-Date: 2024-01-01T00:00:00Z\r\n"
                               b"WARC-Record-ID: <urn:uuid:x>\r\n"
                               b"Content-Length: 0\r\n\r\n\r\n\r\n")
    assert resp.status_code == 200, resp.text
    assert resp.json()["segmented_series_rechecked"][0]["status"] == "incomplete"
    info = segments(client).json()
    assert info["status"] == "incomplete"
    assert info["reason"] == "missing_last_segment"
    assert changes(client)["unevidenced"] == ["2024-03-01T09:00:00Z"]


def test_deleting_continuation_volume_removes_evidence(client):
    put(client, "vol1", series_warc())
    assert segments(client).json()["status"] == "verified"
    resp = client.delete("/volumes/vol1")
    assert resp.status_code == 200
    info = segments(client).json()
    assert info["status"] == "absent"
    assert info["segments"] == []
    assert changes(client)["unevidenced"] == []


def test_removing_required_segment_makes_first_segment_unevidenced(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    put(client, "vol1", first_segment(URL, "2024-03-01T09:00:00Z", head,
                                      b32(V2), FIRST_ID, total_length=total))
    put(client, "vol2", continuation(URL, "2024-03-01T09:00:00Z", tail,
                                     FIRST_ID, 2, total_length=total))
    put(client, "vol3", revisit_record(URL, "2024-09-01T09:00:00Z", b32(V2)))
    assert changes(client)["unevidenced"] == []
    assert client.delete("/volumes/vol2").status_code == 200
    out = changes(client)
    assert out["unevidenced"] == ["2024-03-01T09:00:00Z",
                                  "2024-09-01T09:00:00Z"]
    assert [c["evidenced"] for c in out["captures"]] == [False, False]
    assert out["captures"][0]["segment_status"] == "incomplete"
    # re-adding the continuation is a new verification, not the deleted one
    put(client, "vol4", continuation(URL, "2024-03-01T09:00:00Z", tail,
                                     FIRST_ID, 2, total_length=total))
    assert changes(client)["unevidenced"] == []


def test_late_verification_cannot_restore_evidence(client):
    put(client, "vol1", series_warc())
    st = client.app.state.store
    snapshot = st.snapshot_series(FIRST_ID)
    assert snapshot is not None
    # a required segment disappears after the snapshot but before the result
    assert client.delete("/volumes/vol1").status_code == 200
    stale = verify_series(FIRST_ID, [
        Segment(origin_id=r["origin_id"], segment_number=r["segment_number"],
                url=r["url"], ts=r["ts"], warc_type=r["warc_type"],
                record_id=r["record_id"], is_first=r["warc_type"] == "response",
                declares_total_length=r["declares_total_length"],
                content=bytes(r["content"]), digest=r["payload_digest"],
                http_status=r["http_status"], volume=r["volume"], id=r["id"])
        for r in snapshot.segments])
    assert stale.verified is True  # the stale computation itself looks fine
    assert st.apply_verification(FIRST_ID, snapshot.revision, stale) is False
    assert st.series_status(FIRST_ID) == "absent"
    assert segments(client).json()["status"] == "absent"
    assert changes(client)["unevidenced"] == []
    # the first segment is gone too; nothing was resurrected
    assert client.get("/volumes").json()["volumes"] == []


def test_stale_recheck_after_swap_is_discarded(client):
    put(client, "vol1", series_warc())
    st = client.app.state.store
    snapshot = st.snapshot_series(FIRST_ID)
    first = snapshot.segments[0]
    # same series, first segment replaced while the verification was running
    put(client, "vol1", first_segment(
        URL, "2024-03-01T09:00:00Z", b"HTTP/1.1 200 OK\r\n\r\n", b32(V2),
        FIRST_ID, total_length=len(http_response(V2))))
    result = verify_series(FIRST_ID, [
        Segment(origin_id=r["origin_id"], segment_number=r["segment_number"],
                url=r["url"], ts=r["ts"], warc_type=r["warc_type"],
                record_id=r["record_id"], is_first=r["warc_type"] == "response",
                declares_total_length=r["declares_total_length"],
                content=bytes(r["content"]), digest=r["payload_digest"],
                http_status=r["http_status"], volume=r["volume"], id=r["id"])
        for r in snapshot.segments])
    assert st.apply_verification(FIRST_ID, snapshot.revision, result) is False
    # current state is not the stale "verified"
    info = segments(client).json()
    assert info["status"] in ("incomplete", "invalid")
    assert info["status"] != "verified"
    assert changes(client)["unevidenced"] == ["2024-03-01T09:00:00Z"]


def test_verified_segmented_capture_evidences_revisit_in_other_volume(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    put(client, "vol1", first_segment(URL, "2024-03-01T09:00:00Z", head,
                                      b32(V2), FIRST_ID, total_length=total))
    put(client, "vol2", continuation(URL, "2024-03-01T09:00:00Z", tail,
                                     FIRST_ID, 2, total_length=total)
        + revisit_record(URL, "2024-06-01T09:00:00Z", b32(V2)))
    out = changes(client)
    assert out["unevidenced"] == []
    # the revisit has the same body, so no change interval is reported
    assert out["changes"] == []
    assert [c["evidenced"] for c in out["captures"]] == [True, True]
    # a corrupt first-segment digest does not back revisits
    put(client, "vol3", first_segment(URL, "2024-09-01T09:00:00Z", b"HTTP/1.1 200 OK\r\n\r\n",
                                      b32(b"forged"), "urn:uuid:forged",
                                      total_length=30)
        + continuation(URL, "2024-09-01T09:00:00Z", b"nope", "urn:uuid:forged", 2,
                       total_length=30)
        + revisit_record(URL, "2024-12-01T09:00:00Z", b32(b"forged")))
    out = changes(client)
    assert "2024-12-01T09:00:00Z" in out["unevidenced"]
    assert "2024-09-01T09:00:00Z" in out["unevidenced"]


def test_changes_reports_segment_status_for_capture(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    put(client, "vol1", first_segment(URL, "2024-03-01T09:00:00Z", head,
                                      b32(V2), FIRST_ID, total_length=total))
    cap = changes(client)["captures"][0]
    assert cap["type"] == "response"
    assert cap["segment_origin"] == FIRST_ID
    assert cap["segment_status"] == "incomplete"
    assert cap["evidenced"] is False
    put(client, "vol2", continuation(URL, "2024-03-01T09:00:00Z", tail,
                                     FIRST_ID, 2, total_length=total))
    cap = changes(client)["captures"][0]
    assert cap["segment_status"] == "verified"
    assert cap["evidenced"] is True


def test_missing_sha1_on_first_segment_is_invalid():
    """Direct guard: a first segment without a sha1 digest cannot verify."""
    segment = Segment(origin_id=FIRST_ID, segment_number=1, url=URL,
                      ts="2024-03-01T09:00:00Z", warc_type="response",
                      record_id=FIRST_ID, is_first=True,
                      declares_total_length=None,
                      content=b"HTTP/1.1 200 OK\r\n\r\n" + V2,
                      digest=None)
    last = Segment(origin_id=FIRST_ID, segment_number=2, url=URL,
                   ts="2024-03-01T09:00:00Z", warc_type="continuation",
                   record_id=SECOND_ID, is_first=False,
                   declares_total_length=len(segment.content), content=b"")
    result = verify_series(FIRST_ID, [segment, last])
    assert result.status == "invalid"
    assert result.reason == "missing_payload_digest"


def test_failed_write_keeps_segment_and_changes_evidence(client):
    put(client, "vol1", series_warc())
    put(client, "vol2", revisit_record(URL, "2024-06-01T09:00:00Z", b32(V2)))
    before = changes(client)
    before_segments = segments(client).json()
    assert before["unevidenced"] == []
    # an upload that would remove the final segment is rejected wholesale
    clash = (response_record(URL, "2024-03-01T09:00:00Z", V1)
             + response_record(URL, "2024-06-01T09:00:00Z", V1))
    resp = put(client, "vol3", clash)
    assert resp.status_code == 409
    assert changes(client) == before
    assert segments(client).json() == before_segments


def test_unrelated_upload_succeeds_while_a_series_is_being_verified(client):
    """Verification holds no write lock: another volume can commit meanwhile."""
    put(client, "vol1", series_warc())
    from app import ingest

    original = ingest._verify_series
    observed = {}
    result_unrelated = {}

    def verify_then_upload(store, origin_ids):
        # runs outside the write transaction; commit an unrelated volume now
        nonlocal result_unrelated
        result_unrelated = replace_volume(
            store, client.app.state.files, "vol9",
            response_record(URL, "2024-12-01T09:00:00Z", V1), "vol9.warc")
        observed["volumes"] = sorted(
            row["volume"] for row in store.list_volumes())
        observed["status_during"] = store.series_status(FIRST_ID)
        return original(store, origin_ids)

    ingest._verify_series = verify_then_upload
    try:
        resp = put(client, "vol1", series_warc())
    finally:
        ingest._verify_series = original
    assert resp.status_code == 200
    assert observed["volumes"] == ["vol1", "vol9"]
    assert observed["status_during"] == "pending"
    # verifying a write that touches no series is a no-op (no nested recheck)
    assert result_unrelated["segmented_series_rechecked"] == []
    assert segments(client).json()["status"] == "verified"


def test_series_listing_includes_segmented_responses(client):
    put(client, "vol1", series_warc())
    listed = client.get("/series").json()["series"]
    assert len(listed) == 1
    assert listed[0]["origin_id"] == FIRST_ID
    assert listed[0]["status"] == "verified"
    assert listed[0]["segment_count"] == 2


def test_duplicate_segment_inside_one_file_rejected(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    warc = (first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID,
                          total_length=total)
            + continuation(URL, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                           total_length=total)
            + continuation(URL, "2024-03-01T09:00:00Z", b"", FIRST_ID, 2,
                           total_length=total))
    assert put(client, "vol1", warc).status_code == 409


def test_conflicting_swap_restores_archive_bytes(client):
    old = series_warc()
    put(client, "vol1", old)
    assert (client.data_dir / "volumes" / "vol1.warc").read_bytes() == old
    # a conflicting volume leaves both the index and the archive untouched
    conflicting = response_record(URL, "2024-03-01T09:00:00Z", V1)
    resp = put(client, "vol2", conflicting)
    assert resp.status_code == 409
    assert (client.data_dir / "volumes" / "vol1.warc").read_bytes() == old
    assert not (client.data_dir / "volumes" / "vol2.warc").exists()
    assert not (client.data_dir / "volumes" / "vol2.warc.prev").exists()
    # replacing an existing volume with a conflicting one restores its bytes
    put(client, "vol2", response_record(URL, "2024-06-01T09:00:00Z", V2))
    resp = put(client, "vol1", response_record(
        URL, "2024-06-01T09:00:00Z", V1))
    assert resp.status_code == 409
    assert (client.data_dir / "volumes" / "vol1.warc").read_bytes() == old
    assert not (client.data_dir / "volumes" / "vol1.warc.prev").exists()
    assert segments(client).json()["status"] == "verified"


def test_segment_fields_on_warc_10_are_not_indexed(client):
    head, tail = split_http(V2, 20)
    total = len(http_response(V2))
    warc = (first_segment(URL, "2024-03-01T09:00:00Z", head, b32(V2), FIRST_ID)
            + continuation(URL, "2024-03-01T09:00:00Z", tail, FIRST_ID, 2,
                           total_length=total)).replace(b"WARC/1.1", b"WARC/1.0")
    resp = put(client, "vol1", warc)
    assert resp.status_code == 200
    body = resp.json()
    assert body["segments"] == 0
    assert body["segmented_responses"] == 0
    assert any("1.1" in s["reason"] for s in body["skipped"])
    assert changes(client)["captures"] == []


def test_segmented_response_conflicts_with_capture_at_same_instant(client):
    put(client, "vol1", response_record(URL, "2024-03-01T09:00:00Z", V1))
    resp = put(client, "vol2", series_warc())
    assert resp.status_code == 409
    assert segments(client).status_code == 404


def test_pending_series_is_verified_at_startup(tmp_path):
    data_dir = tmp_path / "data"
    store = Store(str(data_dir / "index.sqlite3"))
    files = VolumeFiles(str(data_dir / "volumes"))
    replace_volume(store, files, "vol1", series_warc(), "vol1.warc")
    assert store.series_status(FIRST_ID) == "verified"
    # simulate a crash between the committed write and its post-commit check
    con = store.connect()
    con.execute("BEGIN IMMEDIATE")
    store._mark_series_pending(con, FIRST_ID, "2024-01-01T00:00:00Z")
    con.execute("UPDATE captures SET valid = 0 WHERE segment_origin = ?",
                (FIRST_ID,))
    con.execute("COMMIT")
    con.close()
    assert store.series_status(FIRST_ID) == "pending"
    assert recheck_pending(store)[0]["status"] == "verified"
    assert store.series_status(FIRST_ID) == "verified"


def test_segment_rows_commit_before_verification_runs(tmp_path, monkeypatch):
    """Reassembly runs after the write transaction, not inside it.

    A concurrent reader (another SQLite connection) must be able to read the
    previous committed state while the write transaction is open, and the
    segment rows must already be durable before verification starts.
    """
    data_dir = tmp_path / "data"
    store = Store(str(data_dir / "index.sqlite3"))
    files = VolumeFiles(str(data_dir / "volumes"))
    put_resp = replace_volume(store, files, "vol1",
                              series_warc(), "v1.warc")
    assert put_resp["segmented_series_rechecked"][0]["status"] == "verified"

    seen = {}
    original_verify = __import__("app.ingest", fromlist=["_verify_series"])._verify_series

    def spy_verify(store_obj, origin_ids):
        con = store_obj.connect()
        seen["segments_visible"] = con.execute(
            "SELECT COUNT(*) FROM segments WHERE origin_id = ?",
            (FIRST_ID,)).fetchone()[0]
        seen["status_before"] = store_obj.series_status(FIRST_ID)
        con.close()
        return original_verify(store_obj, origin_ids)

    monkeypatch.setattr("app.ingest._verify_series", spy_verify)
    # replace the volume with the same series -> post-commit recheck runs
    replace_volume(store, files, "vol1", series_warc(), "v1.warc")
    assert seen["segments_visible"] == 2
    assert seen["status_before"] == "pending"
    assert store.series_status(FIRST_ID) == "verified"


def test_failed_post_commit_recheck_keeps_committed_volume_and_retries(tmp_path,
                                                                      monkeypatch):
    """A crash during verification must not report the write as failed.

    The volume rows are committed before verification, so the caller's upload
    succeeded; the touched series is left pending and startup (or the next
    write) completes the check.
    """
    data_dir = tmp_path / "data"
    store = Store(str(data_dir / "index.sqlite3"))
    files = VolumeFiles(str(data_dir / "volumes"))
    from app import ingest

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash during reassembly")

    monkeypatch.setattr(ingest, "verify_series", boom)
    result = replace_volume(store, files, "vol1", series_warc(), "v.warc")
    assert "recheck deferred" in result["segmented_series_rechecked"][0]["error"]
    assert store.series_status(FIRST_ID) == "pending"
    assert files.path("vol1").endswith("vol1.warc")
    monkeypatch.undo()
    # startup recovery finishes exactly that pending check
    assert recheck_pending(store)[0]["status"] == "verified"
    assert store.series_status(FIRST_ID) == "verified"


def _seg(number, warc_type, content, total=None, url=URL, digest=None,
         record_id=None):
    return Segment(origin_id=FIRST_ID, segment_number=number, url=url,
                   ts="2024-03-01T09:00:00Z", warc_type=warc_type,
                   record_id=record_id, is_first=warc_type == "response",
                   declares_total_length=total, content=content,
                   digest=digest)


def test_verify_rejects_continuation_as_first_segment():
    http = http_response(V2)
    result = verify_series(FIRST_ID, [
        _seg(1, "continuation", http, total=len(http)),
    ])
    assert result.status == "invalid"
    assert result.reason == "continuation_cannot_be_first"


def test_verify_rejects_multiple_first_segments():
    http = http_response(V2)
    half = len(http) // 2
    result = verify_series(FIRST_ID, [
        _seg(1, "response", http[:half], digest=b32(V2)),
        _seg(1, "response", http[:half], digest=b32(V2)),
        _seg(2, "continuation", http[half:], total=len(http)),
    ])
    assert result.status == "invalid"
    assert result.reason == "multiple_first_segments"


def test_verify_rejects_last_segment_that_is_not_final():
    http = http_response(V2)
    half = len(http) // 2
    result = verify_series(FIRST_ID, [
        _seg(1, "response", http[:half], digest=b32(V2)),
        _seg(2, "continuation", http[half:], total=len(http)),
        _seg(3, "continuation", b"extra"),
    ])
    assert result.status == "invalid"
    assert result.reason == "last_segment_not_final"


def test_verify_requires_the_declared_length_to_be_on_a_continuation():
    http = http_response(V2)
    # a lone first segment cannot declare the total length itself
    result = verify_series(FIRST_ID, [
        _seg(1, "response", http, total=len(http), digest=b32(V2)),
    ])
    assert result.status == "invalid"
    assert result.reason == "last_segment_not_continuation"
    # when a later segment exists, the declaration is simply not on the final one
    result = verify_series(FIRST_ID, [
        _seg(1, "response", http, total=len(http), digest=b32(V2)),
        _seg(2, "continuation", b""),
    ])
    assert result.status == "invalid"
    assert result.reason == "last_segment_not_final"


def test_verify_rejects_reassembled_message_that_is_not_http():
    result = verify_series(FIRST_ID, [
        _seg(1, "response", b"not-an-http-message", digest=b32(V2)),
        _seg(2, "continuation", b"tail", total=len(b"not-an-http-messagetail")),
    ])
    assert result.status == "invalid"
    assert result.reason == "malformed_http_response"


def test_verify_rejects_encoded_reassembled_body():
    http = http_response(V2, extra_headers=("Content-Encoding: gzip",))
    half = len(http) // 2
    result = verify_series(FIRST_ID, [
        _seg(1, "response", http[:half], digest=b32(V2)),
        _seg(2, "continuation", http[half:], total=len(http)),
    ])
    assert result.status == "invalid"
    assert result.reason == "http_body_encoded"
