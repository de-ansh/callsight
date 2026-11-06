"""Postgres persistence for analyzed calls (schema: db/schema.sql). Requires psycopg."""

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


def save_call(conn: psycopg.Connection, transcript: Transcript, report: CallReport, source: str = "upload") -> UUID:
    """Insert the call, its segments and insights in one transaction; returns the new call id."""
    with conn.transaction():
        call_id = conn.execute(
            "INSERT INTO calls (external_id, source, duration_s, summary, talk_share)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (report.call_id, source, report.duration_s, report.summary, Jsonb(report.talk_share)),
        ).fetchone()["id"]
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
    return call_id


def list_calls(conn: psycopg.Connection, limit: int, offset: int) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT c.id, c.external_id, c.source, c.duration_s, c.created_at,"
        " (SELECT count(*) FROM insights i WHERE i.call_id = c.id) AS insight_count"
        " FROM calls c ORDER BY c.created_at DESC, c.id LIMIT %s OFFSET %s",
        (limit, offset),
    ).fetchall()


def get_call(conn: psycopg.Connection, call_id: UUID) -> dict[str, Any] | None:
    """The stored call in the same shape as CallReport.to_dict(), plus id, timestamps and segments."""
    call = conn.execute(
        "SELECT id, external_id, source, duration_s, summary, talk_share, created_at FROM calls WHERE id = %s",
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
        "created_at": call["created_at"],
        "duration_s": call["duration_s"],
        "speakers": list(dict.fromkeys(s["speaker"] for s in segments)),
        "talk_share": call["talk_share"],
        "summary": call["summary"],
        "insights": insights,
        "segments": segments,
    }
