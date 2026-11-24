"""SQLite storage: schema, volume swap, segment bookkeeping, queries.

Evidence model
--------------

* An ordinary response is digest-verified at ingest; ``captures.valid`` is
  its evidence flag.
* A segmented response (WARC 1.1 ``WARC-Segment-Number`` records and
  ``continuation`` records) lives in ``segments`` and is verified only after
  a volume change has been committed, outside the write transaction.  A
  ``series`` row tracks the verification status and a monotonically
  increasing ``revision`` for the segment set.  Verification results are
  applied with an optimistic guard on that revision, so a result computed
  before a volume swap/delete can never re-evidence a series whose segment
  set has changed since (a late verification cannot restore evidence).
* Revisit evidence remains derived at query time from digest-verified
  responses, so a verified segmented capture automatically backs matching
  revisits in other volumes.

Only segmented series touched by a volume swap/delete are re-verified; the
write transaction itself is short, and reassembly/hashing happens after it
commits, so unrelated volume uploads are not blocked by a concatenation
check.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS volumes (
    volume      TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    records     INTEGER NOT NULL,
    segments    INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
    url         TEXT NOT NULL,
    ts          TEXT NOT NULL,          -- canonical UTC ISO-8601 capture instant
    volume      TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('response', 'revisit')),
    digest      TEXT NOT NULL,          -- canonical sha1/base32 payload digest
    valid       INTEGER,                -- response: 1 = digest verified, 0 = failed; revisit: NULL
    http_status INTEGER,
    segment_origin TEXT,                -- first segment's record id for a segmented response
    PRIMARY KEY (url, ts)               -- one capture per URL per UTC instant, across all volumes
);

CREATE INDEX IF NOT EXISTS idx_captures_volume ON captures(volume);

-- Evidence lookup for revisits: verified response bodies by digest.
CREATE INDEX IF NOT EXISTS idx_verified_response_digest
    ON captures(digest) WHERE type = 'response' AND valid = 1;

-- One row per WARC segment.  A segmented series is keyed by the first
-- segment's WARC-Record-ID (continuations point at it with
-- WARC-Segment-Origin-ID), which lets a continuation arrive before its
-- first segment.
CREATE TABLE IF NOT EXISTS segments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_id             TEXT NOT NULL,
    segment_number        INTEGER NOT NULL,
    volume                TEXT NOT NULL,
    warc_type             TEXT NOT NULL CHECK (warc_type IN ('response', 'continuation')),
    record_id             TEXT,
    url                   TEXT NOT NULL,
    ts                    TEXT NOT NULL,
    declares_total_length INTEGER,
    payload_digest        TEXT,
    http_status           INTEGER,
    content               BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_volume ON segments(volume);
CREATE INDEX IF NOT EXISTS idx_segments_origin
    ON segments(origin_id, segment_number);
CREATE INDEX IF NOT EXISTS idx_segments_record ON segments(record_id);

-- Verification status per segmented series.  Rows are kept as tombstones
-- (status 'absent', no segments) so revisions never regress across a
-- delete/re-upload cycle.
CREATE TABLE IF NOT EXISTS series (
    origin_id       TEXT PRIMARY KEY,
    first_record_id TEXT,
    revision        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    reason          TEXT,
    detail          TEXT,
    capture_url     TEXT,
    capture_ts      TEXT,
    digest          TEXT,
    first_volume    TEXT,
    http_status     INTEGER,
    total_length    INTEGER,
    assembled_length INTEGER,
    problems_json   TEXT,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_series_status ON series(status);
"""

# One round trip per URL; revisit evidence resolved against the *current*
# committed state of the verified-response set.
CAPTURES_FOR_URL_SQL = """
SELECT c.ts, c.type, c.digest, c.valid, c.volume, c.http_status,
       c.segment_origin, s.status AS segment_status, s.reason AS segment_reason,
       CASE WHEN c.type = 'response' THEN c.valid
            ELSE EXISTS(SELECT 1 FROM captures r
                        WHERE r.type = 'response' AND r.valid = 1
                              AND r.digest = c.digest)
       END AS evidenced
FROM captures c
LEFT JOIN series s ON s.origin_id = c.segment_origin
WHERE c.url = ?
ORDER BY c.ts ASC
"""

SEGMENTS_FOR_ORIGIN_SQL = """
SELECT id, origin_id, segment_number, volume, warc_type, record_id,
       url, ts, declares_total_length, payload_digest, http_status, content
FROM segments
WHERE origin_id = ?
ORDER BY segment_number ASC, volume ASC, id ASC
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


@dataclass
class SwapResult:
    conflicts: list[dict] = field(default_factory=list)
    affected_origins: list[str] = field(default_factory=list)
    removed_origins: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    origin_id: str
    revision: int
    status: str
    segments: list[sqlite3.Row]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        con = self.connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            # Migrate databases created by earlier versions of the service.
            _ensure_column(con, "volumes", "segments",
                           "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(con, "captures", "segment_origin", "TEXT")
        finally:
            con.close()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.isolation_level = None  # autocommit; transactions are explicit
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    # -- ingest ------------------------------------------------------------

    def swap_volume(self, volume: str, captures, segments, volume_row: dict) -> SwapResult:
        """Replace *volume* wholesale in one transaction.

        Captures of *this* volume are deleted first, so replacing a volume
        never conflicts with its own old rows.  Conflicts with other volumes
        abort without writing anything.  Any exception rolls back, so the
        old volume (index and archived file) survives a failed swap.

        Every segmented series touched by the old or the new volume is
        marked ``pending`` with a bumped revision and its captures lose
        evidence in the same transaction; the caller re-verifies those
        series after the transaction has committed.
        """
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            old_origins = {
                row["origin_id"] for row in con.execute(
                    "SELECT DISTINCT origin_id FROM segments WHERE volume = ?",
                    (volume,))
            }
            con.execute("DELETE FROM captures WHERE volume = ?", (volume,))
            con.execute("DELETE FROM segments WHERE volume = ?", (volume,))

            conflicts = []
            for cap in captures:
                row = con.execute(
                    "SELECT volume FROM captures WHERE url = ? AND ts = ?",
                    (cap.url, cap.ts),
                ).fetchone()
                if row is not None:
                    conflicts.append({"url": cap.url, "ts": cap.ts,
                                      "held_by": row["volume"]})
            if conflicts:
                con.execute("ROLLBACK")
                return SwapResult(conflicts=conflicts)

            con.executemany(
                "INSERT INTO captures (url, ts, volume, type, digest, valid,"
                " http_status, segment_origin)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (c.url, c.ts, volume, c.type, c.digest, c.valid,
                     c.http_status, getattr(c, "segment_origin", None))
                    for c in captures
                ],
            )
            con.executemany(
                "INSERT INTO segments (origin_id, segment_number, volume,"
                " warc_type, record_id, url, ts, declares_total_length,"
                " payload_digest, http_status, content)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (s.origin_id, s.segment_number, volume, s.warc_type,
                     s.record_id, s.url, s.ts, s.declares_total_length,
                     s.digest, s.http_status, s.content)
                    for s in segments
                ],
            )
            con.execute(
                "INSERT INTO volumes (volume, filename, sha256, size_bytes,"
                " records, segments, ingested_at)"
                " VALUES (:volume, :filename, :sha256, :size_bytes,"
                " :records, :segments, :ingested_at)"
                " ON CONFLICT(volume) DO UPDATE SET"
                "   filename = excluded.filename, sha256 = excluded.sha256,"
                "   size_bytes = excluded.size_bytes, records = excluded.records,"
                "   segments = excluded.segments,"
                "   ingested_at = excluded.ingested_at",
                {"volume": volume, **volume_row},
            )

            new_origins = {s.origin_id for s in segments}
            affected = sorted(old_origins | new_origins)
            removed = []
            now = _utcnow()
            for origin_id in affected:
                remaining = con.execute(
                    "SELECT COUNT(*) FROM segments WHERE origin_id = ?",
                    (origin_id,)).fetchone()[0]
                if remaining == 0:
                    removed.append(origin_id)
                    self._mark_series_absent(con, origin_id, now)
                else:
                    self._mark_series_pending(con, origin_id, now)
                con.execute(
                    "UPDATE captures SET valid = 0 WHERE segment_origin = ?",
                    (origin_id,))
            con.execute("COMMIT")
            return SwapResult(affected_origins=affected, removed_origins=removed)
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _mark_series_pending(self, con: sqlite3.Connection, origin_id: str,
                             now: str) -> None:
        first = con.execute(
            "SELECT record_id, url, ts, payload_digest, http_status, volume"
            " FROM segments WHERE origin_id = ?"
            " AND warc_type = 'response' AND segment_number = 1"
            " ORDER BY id ASC LIMIT 1",
            (origin_id,),
        ).fetchone()
        values = {
            "origin_id": origin_id,
            "first_record_id": first["record_id"] if first else None,
            "capture_url": first["url"] if first else None,
            "capture_ts": first["ts"] if first else None,
            "digest": first["payload_digest"] if first else None,
            "http_status": first["http_status"] if first else None,
            "first_volume": first["volume"] if first else None,
            "updated_at": now,
        }
        updated = con.execute(
            "UPDATE series SET revision = revision + 1, status = 'pending',"
            " reason = 'awaiting_recheck', detail = NULL, problems_json = '[]',"
            " first_record_id = :first_record_id,"
            " capture_url = :capture_url, capture_ts = :capture_ts,"
            " digest = :digest, http_status = :http_status,"
            " first_volume = :first_volume,"
            " total_length = NULL, assembled_length = NULL,"
            " updated_at = :updated_at"
            " WHERE origin_id = :origin_id",
            values,
        ).rowcount
        if not updated:
            con.execute(
                "INSERT INTO series (origin_id, first_record_id, revision,"
                " status, reason, detail, capture_url, capture_ts, digest,"
                " first_volume, http_status, total_length, assembled_length,"
                " problems_json, updated_at)"
                " VALUES (:origin_id, :first_record_id, 0, 'pending',"
                " 'awaiting_recheck', NULL, :capture_url, :capture_ts,"
                " :digest, :first_volume, :http_status, NULL, NULL, '[]',"
                " :updated_at)",
                values,
            )

    def _mark_series_absent(self, con: sqlite3.Connection, origin_id: str,
                            now: str) -> None:
        updated = con.execute(
            "UPDATE series SET revision = revision + 1, status = 'absent',"
            " reason = NULL, detail = NULL, problems_json = '[]',"
            " capture_url = NULL, capture_ts = NULL, digest = NULL,"
            " first_volume = NULL, http_status = NULL,"
            " total_length = NULL, assembled_length = NULL,"
            " updated_at = ? WHERE origin_id = ?",
            (now, origin_id),
        ).rowcount
        if not updated:
            con.execute(
                "INSERT INTO series (origin_id, first_record_id, revision,"
                " status, reason, detail, capture_url, capture_ts, digest,"
                " first_volume, http_status, total_length, assembled_length,"
                " problems_json, updated_at)"
                " VALUES (?, NULL, 0, 'absent', NULL, NULL, NULL, NULL, NULL,"
                " NULL, NULL, NULL, NULL, '[]', ?)",
                (origin_id, now),
            )

    def delete_volume(self, volume: str) -> tuple[bool, list[str]]:
        """Remove a volume; returns (found, affected segmented series)."""
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT 1 FROM volumes WHERE volume = ?",
                              (volume,)).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                return False, []
            old_origins = {
                r["origin_id"] for r in con.execute(
                    "SELECT DISTINCT origin_id FROM segments WHERE volume = ?",
                    (volume,))
            }
            con.execute("DELETE FROM captures WHERE volume = ?", (volume,))
            con.execute("DELETE FROM segments WHERE volume = ?", (volume,))
            con.execute("DELETE FROM volumes WHERE volume = ?", (volume,))
            now = _utcnow()
            removed = []
            for origin_id in sorted(old_origins):
                remaining = con.execute(
                    "SELECT COUNT(*) FROM segments WHERE origin_id = ?",
                    (origin_id,)).fetchone()[0]
                if remaining == 0:
                    removed.append(origin_id)
                    self._mark_series_absent(con, origin_id, now)
                else:
                    self._mark_series_pending(con, origin_id, now)
                con.execute(
                    "UPDATE captures SET valid = 0 WHERE segment_origin = ?",
                    (origin_id,))
            con.execute("COMMIT")
            return True, sorted(old_origins)
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    # -- segmented-series verification snapshots / results ----------------

    def snapshot_series(self, origin_id: str) -> Snapshot | None:
        """Read one series and all its segments in one consistent snapshot."""
        con = self.connect()
        try:
            con.execute("BEGIN")
            series = con.execute(
                "SELECT * FROM series WHERE origin_id = ?", (origin_id,)
            ).fetchone()
            if series is None:
                con.execute("COMMIT")
                return None
            segments = con.execute(SEGMENTS_FOR_ORIGIN_SQL,
                                   (origin_id,)).fetchall()
            con.execute("COMMIT")
            return Snapshot(origin_id=origin_id, revision=series["revision"],
                            status=series["status"], segments=segments)
        finally:
            con.close()

    def apply_verification(self, origin_id: str, revision: int,
                           result, now: str | None = None) -> bool:
        """Apply a verification result if the segment set is still current.

        The result is discarded when the revision changed (a volume swap or
        delete touched this series) or when another verification already
        applied.  This is what prevents a late concatenation check from
        restoring evidence after a required segment was removed.
        """
        now = now or _utcnow()
        problems = json.dumps([p.as_dict() for p in result.problems],
                              ensure_ascii=False)
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT revision, status FROM series WHERE origin_id = ?",
                (origin_id,),
            ).fetchone()
            if (row is None or row["revision"] != revision
                    or row["status"] != "pending"):
                con.execute("ROLLBACK")
                return False
            con.execute(
                "UPDATE series SET status = ?, reason = ?, detail = ?,"
                " problems_json = ?, total_length = ?, assembled_length = ?,"
                " http_status = COALESCE(?, http_status), first_volume ="
                " COALESCE(?, first_volume), digest = COALESCE(?, digest),"
                " capture_url = COALESCE(?, capture_url),"
                " capture_ts = COALESCE(?, capture_ts), updated_at = ?"
                " WHERE origin_id = ? AND revision = ? AND status = 'pending'",
                (result.status, result.reason, result.detail, problems,
                 result.total_length, result.assembled_length,
                 result.http_status, result.first_volume, result.digest,
                 result.capture_url, result.capture_ts, now, origin_id,
                 revision),
            )
            con.execute(
                "UPDATE captures SET valid = ?, http_status = COALESCE(?,"
                " http_status) WHERE segment_origin = ?",
                (1 if result.verified else 0, result.http_status, origin_id),
            )
            con.execute("COMMIT")
            return True
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def pending_origins(self) -> list[str]:
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT origin_id FROM series WHERE status = 'pending'"
                " ORDER BY origin_id")
            return [row["origin_id"] for row in rows]
        finally:
            con.close()

    def series_status(self, origin_id: str) -> str | None:
        con = self.connect()
        try:
            row = con.execute("SELECT status FROM series WHERE origin_id = ?",
                              (origin_id,)).fetchone()
            return row["status"] if row else None
        finally:
            con.close()

    def list_series(self) -> list[dict]:
        con = self.connect()
        try:
            con.execute("BEGIN")
            rows = con.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM segments g"
                "            WHERE g.origin_id = s.origin_id) AS segment_count"
                " FROM series s ORDER BY s.capture_ts ASC, s.origin_id ASC"
            ).fetchall()
            con.execute("COMMIT")
            return [dict(row) for row in rows]
        finally:
            con.close()

    # -- queries (each runs inside a single read transaction, so every -----
    # -- query sees exactly one committed state of the index) ---------------

    def list_volumes(self) -> list[sqlite3.Row]:
        con = self.connect()
        try:
            con.execute("BEGIN")
            rows = con.execute(
                "SELECT * FROM volumes ORDER BY volume"
            ).fetchall()
            con.execute("COMMIT")
            return rows
        finally:
            con.close()

    def captures_for_url(self, url: str) -> list[sqlite3.Row]:
        con = self.connect()
        try:
            con.execute("BEGIN")
            rows = con.execute(CAPTURES_FOR_URL_SQL, (url,)).fetchall()
            con.execute("COMMIT")
            return rows
        finally:
            con.close()

    def resolve_origin_id(self, record_id: str) -> str | None:
        """Resolve a first-segment record ID (or any segment's ID) to an origin."""
        con = self.connect()
        try:
            con.execute("BEGIN")
            row = con.execute(
                "SELECT origin_id FROM series WHERE origin_id = ?"
                " OR first_record_id = ? LIMIT 1",
                (record_id, record_id),
            ).fetchone()
            if row is None:
                row = con.execute(
                    "SELECT origin_id FROM segments WHERE record_id = ?"
                    " ORDER BY id ASC LIMIT 1", (record_id,)
                ).fetchone()
            con.execute("COMMIT")
            return row["origin_id"] if row else None
        finally:
            con.close()

    def series_info(self, origin_id: str) -> dict | None:
        """Series row, segments and the first-segment capture, in one read txn."""
        con = self.connect()
        try:
            con.execute("BEGIN")
            series = con.execute(
                "SELECT * FROM series WHERE origin_id = ?", (origin_id,)
            ).fetchone()
            if series is None:
                con.execute("COMMIT")
                return None
            segments = con.execute(SEGMENTS_FOR_ORIGIN_SQL,
                                   (origin_id,)).fetchall()
            capture = con.execute(
                "SELECT url, ts, volume, type, digest, valid, http_status"
                " FROM captures WHERE segment_origin = ?"
                " ORDER BY ts ASC, volume ASC LIMIT 1",
                (origin_id,),
            ).fetchone()
            con.execute("COMMIT")
            return {"series": series, "segments": segments,
                    "capture": capture}
        finally:
            con.close()
