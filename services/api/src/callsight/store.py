"""Postgres persistence for analyzed calls (schema: callsight/migrations). Requires psycopg."""

from __future__ import annotations

import atexit
import threading
from contextlib import AbstractContextManager
from dataclasses import asdict
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .models import CallReport, Segment, Transcript

POOL_MAX_SIZE = 10  # per process; FastAPI's threadpool waits for a free connection beyond this
_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


@atexit.register
def _close_pools() -> None:
    for pool in _pools.values():
        pool.close(timeout=2)


def connect(database_url: str) -> AbstractContextManager[psycopg.Connection]:
    """A pooled connection: `with connect(url) as conn:` commits on success, rolls back on error,
    and returns the connection to the pool."""
    pool = _pools.get(database_url)
    if pool is None:
        with _pools_lock:
            pool = _pools.get(database_url)
            if pool is None:
                pool = ConnectionPool(
                    database_url, kwargs={"row_factory": dict_row}, min_size=1, max_size=POOL_MAX_SIZE,
                    check=ConnectionPool.check_connection,  # survives a database restart
                    open=True, name="callsight",
                )
                _pools[database_url] = pool
    return pool.connection()


# A job claimed longer ago than this is assumed to belong to a crashed worker and is retried.
STALE_CLAIM = "2 hours"


def _insert_results(conn: psycopg.Connection, call_id: UUID, transcript: Transcript, report: CallReport) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO segments (call_id, idx, start_s, speaker, text) VALUES (%s, %s, %s, %s, %s)",
            [(call_id, s.id, s.start_s, s.speaker, s.text) for s in transcript.segments],
        )
        cur.executemany(
            "INSERT INTO insights (call_id, idx, kind, severity, title, detail, evidence)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (call_id, i, ins.kind, ins.severity, ins.title, ins.detail,
                 Jsonb([asdict(e) for e in ins.evidence]))
                for i, ins in enumerate(report.insights)
            ],
        )


def save_call(
    conn: psycopg.Connection, org_id: UUID, transcript: Transcript, report: CallReport, source: str = "text"
) -> UUID:
    """Insert the call, its segments and insights in one transaction; returns the new call id."""
    with conn.transaction():
        call_id = conn.execute(
            "INSERT INTO calls (org_id, external_id, source, duration_s, summary, talk_share)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (org_id, report.call_id, source, report.duration_s, report.summary, Jsonb(report.talk_share)),
        ).fetchone()["id"]
        _insert_results(conn, call_id, transcript, report)
    return call_id


def create_queued_call(
    conn: psycopg.Connection, org_id: UUID, call_id: UUID, external_id: str, recording_key: str | None = None,
    recording_url: str | None = None, source: str = "audio",
) -> None:
    """Register a recording for the worker: already in storage (key) or to be fetched (url)."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO calls (id, org_id, external_id, source, status, recording_key, recording_url)"
            " VALUES (%s, %s, %s, %s, 'queued', %s, %s)",
            (call_id, org_id, external_id, source, recording_key, recording_url),
        )


def set_recording_key(conn: psycopg.Connection, call_id: UUID, key: str) -> None:
    with conn.transaction():
        conn.execute("UPDATE calls SET recording_key = %s WHERE id = %s", (key, call_id))


def recording_key(conn: psycopg.Connection, org_id: UUID, call_id: UUID) -> str | None:
    row = conn.execute(
        "SELECT recording_key FROM calls WHERE id = %s AND org_id = %s", (call_id, org_id)
    ).fetchone()
    return row["recording_key"] if row else None


def audit(
    conn: psycopg.Connection, org_id: UUID, actor: str, action: str, target: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO audit_log (org_id, actor, action, target, details) VALUES (%s, %s, %s, %s, %s)",
            (org_id, actor, action, target, Jsonb(details or {})),
        )


def list_audit(conn: psycopg.Connection, org_id: UUID, limit: int, offset: int) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT actor, action, target, details, at FROM audit_log WHERE org_id = %s"
        " ORDER BY at DESC, id DESC LIMIT %s OFFSET %s",
        (org_id, limit, offset),
    ).fetchall()


def call_storage(conn: psycopg.Connection, org_id: UUID, call_id: UUID) -> dict[str, Any] | None:
    """The call's recording key ({'recording_key': None} without one); None if the call is not in this org."""
    return conn.execute(
        "SELECT recording_key FROM calls WHERE id = %s AND org_id = %s", (call_id, org_id)
    ).fetchone()


def delete_call(conn: psycopg.Connection, org_id: UUID, call_id: UUID) -> bool:
    """Delete the call row; segments and insights go with it (ON DELETE CASCADE)."""
    with conn.transaction():
        return conn.execute(
            "DELETE FROM calls WHERE id = %s AND org_id = %s", (call_id, org_id)
        ).rowcount == 1


def expired_calls(conn: psycopg.Connection, days: int, limit: int = 100) -> list[dict[str, Any]]:
    """Calls older than `days`, across all orgs. Jobs being processed are left for the next round."""
    return conn.execute(
        "SELECT id, org_id FROM calls WHERE created_at < now() - make_interval(days => %s)"
        " AND status <> 'processing' ORDER BY created_at LIMIT %s",
        (days, limit),
    ).fetchall()


def org_competitors(conn: psycopg.Connection, org_id: UUID) -> tuple[str, ...]:
    row = conn.execute("SELECT competitors FROM orgs WHERE id = %s", (org_id,)).fetchone()
    return tuple(row["competitors"]) if row else ()


def set_org_competitors(conn: psycopg.Connection, org_id: UUID, competitors: list[str]) -> None:
    with conn.transaction():
        conn.execute("UPDATE orgs SET competitors = %s WHERE id = %s", (competitors, org_id))


def org_webhook_secret(conn: psycopg.Connection, org_id: UUID) -> str | None:
    row = conn.execute("SELECT webhook_secret FROM orgs WHERE id = %s", (org_id,)).fetchone()
    return row["webhook_secret"] if row else None


def claim_job(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Atomically take the oldest queued (or stale) audio job; safe with several workers."""
    with conn.transaction():
        return conn.execute(
            "UPDATE calls SET status = 'processing', claimed_at = now(), error = NULL"
            " WHERE id = (SELECT id FROM calls"
            "   WHERE status = 'queued'"
            f"     OR (status = 'processing' AND claimed_at < now() - interval '{STALE_CLAIM}')"
            "   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)"
            " RETURNING id, org_id, external_id, recording_key, recording_url"
        ).fetchone()


def complete_job(conn: psycopg.Connection, call_id: UUID, transcript: Transcript, report: CallReport) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE calls SET status = 'done', duration_s = %s, summary = %s, talk_share = %s WHERE id = %s",
            (report.duration_s, report.summary, Jsonb(report.talk_share), call_id),
        )
        # A stale claim can be retried while the first worker still finishes; last write wins.
        conn.execute("DELETE FROM segments WHERE call_id = %s", (call_id,))
        conn.execute("DELETE FROM insights WHERE call_id = %s", (call_id,))
        _insert_results(conn, call_id, transcript, report)


def load_transcript(conn: psycopg.Connection, org_id: UUID, call_id: UUID) -> tuple[Transcript, dict[str, Any]] | None:
    """The stored transcript of a call in this org, plus its row (status, external_id, rep_speaker)."""
    call = conn.execute(
        "SELECT id, external_id, status, rep_speaker FROM calls WHERE id = %s AND org_id = %s", (call_id, org_id)
    ).fetchone()
    if call is None:
        return None
    rows = conn.execute(
        "SELECT idx, start_s, speaker, text FROM segments WHERE call_id = %s ORDER BY idx", (call_id,)
    ).fetchall()
    segments = [Segment(r["idx"], r["start_s"], r["speaker"], r["text"]) for r in rows]
    return Transcript(call["external_id"] or str(call_id), segments), call


def replace_analysis(
    conn: psycopg.Connection, call_id: UUID, transcript: Transcript, report: CallReport, rep_speaker: str | None
) -> None:
    """Store a fresh analysis (and possibly renamed speakers) for an existing call, atomically."""
    with conn.transaction():
        conn.execute(
            "UPDATE calls SET summary = %s, talk_share = %s, duration_s = %s, rep_speaker = %s WHERE id = %s",
            (report.summary, Jsonb(report.talk_share), report.duration_s, rep_speaker, call_id),
        )
        conn.execute("DELETE FROM segments WHERE call_id = %s", (call_id,))
        conn.execute("DELETE FROM insights WHERE call_id = %s", (call_id,))
        _insert_results(conn, call_id, transcript, report)


def fail_job(conn: psycopg.Connection, call_id: UUID, error: str) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE calls SET status = 'failed', error = %s WHERE id = %s AND status <> 'done'",
            (error[:2000], call_id),
        )


def list_calls(
    conn: psycopg.Connection, org_id: UUID, limit: int, offset: int,
    query: str | None = None, status: str | None = None,
) -> list[dict[str, Any]]:
    """Newest first, with insight counts per severity. `query` matches the call name or anything said."""
    where, params = ["c.org_id = %s"], [org_id]
    if status:
        where.append("c.status = %s")
        params.append(status)
    if query:
        # shortcut: ILIKE scans every segment; add a trigram or full-text index when orgs hold many calls.
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(c.external_id ILIKE %s OR EXISTS (SELECT 1 FROM segments s WHERE s.call_id = c.id AND s.text ILIKE %s))")
        params += [pattern, pattern]
    return conn.execute(
        "SELECT c.id, c.external_id, c.source, c.status, c.duration_s, c.created_at,"
        " count(i.*) AS insight_count,"
        " count(i.*) FILTER (WHERE i.severity = 'high') AS high_count,"
        " count(i.*) FILTER (WHERE i.severity = 'medium') AS medium_count"
        " FROM calls c LEFT JOIN insights i ON i.call_id = c.id"
        f" WHERE {' AND '.join(where)}"
        " GROUP BY c.id ORDER BY c.created_at DESC, c.id LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()


def get_call(conn: psycopg.Connection, org_id: UUID, call_id: UUID) -> dict[str, Any] | None:
    """The stored call in the same shape as CallReport.to_dict(), plus id, timestamps and segments."""
    call = conn.execute(
        "SELECT id, external_id, source, status, error, duration_s, summary, talk_share, created_at, rep_speaker,"
        " recording_key IS NOT NULL AS has_recording"
        " FROM calls WHERE id = %s AND org_id = %s",
        (call_id, org_id),
    ).fetchone()
    if call is None:
        return None
    segments = conn.execute(
        "SELECT idx AS id, start_s, speaker, text FROM segments WHERE call_id = %s ORDER BY idx", (call_id,)
    ).fetchall()
    insights = conn.execute(
        "SELECT kind, title, detail, severity, evidence FROM insights WHERE call_id = %s ORDER BY idx",
        (call_id,),
    ).fetchall()
    return {
        "id": call["id"],
        "call_id": call["external_id"],
        "source": call["source"],
        "status": call["status"],
        "error": call["error"],
        "has_recording": call["has_recording"],
        "rep_speaker": call["rep_speaker"],
        "created_at": call["created_at"],
        "duration_s": call["duration_s"],
        "speakers": list(dict.fromkeys(s["speaker"] for s in segments)),
        "talk_share": call["talk_share"],
        "summary": call["summary"],
        "insights": insights,
        "segments": segments,
    }
