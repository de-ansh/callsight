"""Postgres persistence for analyzed calls (schema: callsight/migrations). Requires psycopg."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import CallReport, Transcript


def connect(database_url: str) -> psycopg.Connection:
    # shortcut: one connection per request, no pool; switch to psycopg_pool when traffic needs it.
    return psycopg.connect(database_url, row_factory=dict_row)


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


def save_call(conn: psycopg.Connection, transcript: Transcript, report: CallReport, source: str = "upload") -> UUID:
    """Insert the call, its segments and insights in one transaction; returns the new call id."""
    with conn.transaction():
        call_id = conn.execute(
            "INSERT INTO calls (external_id, source, duration_s, summary, talk_share)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (report.call_id, source, report.duration_s, report.summary, Jsonb(report.talk_share)),
        ).fetchone()["id"]
        _insert_results(conn, call_id, transcript, report)
    return call_id


def create_queued_call(conn: psycopg.Connection, call_id: UUID, external_id: str, recording_key: str) -> None:
    """Register an uploaded recording for the worker to process."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO calls (id, external_id, source, status, recording_key) VALUES (%s, %s, 'audio', 'queued', %s)",
            (call_id, external_id, recording_key),
        )


def claim_job(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Atomically take the oldest queued (or stale) audio job; safe with several workers."""
    with conn.transaction():
        return conn.execute(
            "UPDATE calls SET status = 'processing', claimed_at = now(), error = NULL"
            " WHERE id = (SELECT id FROM calls"
            "   WHERE status = 'queued'"
            f"     OR (status = 'processing' AND claimed_at < now() - interval '{STALE_CLAIM}')"
            "   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)"
            " RETURNING id, external_id, recording_key"
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


def fail_job(conn: psycopg.Connection, call_id: UUID, error: str) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE calls SET status = 'failed', error = %s WHERE id = %s AND status <> 'done'",
            (error[:2000], call_id),
        )


def list_calls(conn: psycopg.Connection, limit: int, offset: int) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT c.id, c.external_id, c.source, c.status, c.duration_s, c.created_at,"
        " (SELECT count(*) FROM insights i WHERE i.call_id = c.id) AS insight_count"
        " FROM calls c ORDER BY c.created_at DESC, c.id LIMIT %s OFFSET %s",
        (limit, offset),
    ).fetchall()


def get_call(conn: psycopg.Connection, call_id: UUID) -> dict[str, Any] | None:
    """The stored call in the same shape as CallReport.to_dict(), plus id, timestamps and segments."""
    call = conn.execute(
        "SELECT id, external_id, source, status, error, duration_s, summary, talk_share, created_at"
        " FROM calls WHERE id = %s",
        (call_id,),
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
        "created_at": call["created_at"],
        "duration_s": call["duration_s"],
        "speakers": list(dict.fromkeys(s["speaker"] for s in segments)),
        "talk_share": call["talk_share"],
        "summary": call["summary"],
        "insights": insights,
        "segments": segments,
    }
