"""Deleting calls: on request (DELETE /v1/calls/{id}) and by retention period (CALLSIGHT_RETENTION_DAYS)."""

from __future__ import annotations

import logging
from uuid import UUID

from . import recordings, store
from .config import Settings

log = logging.getLogger("callsight.retention")


def delete_call(settings: Settings, org_id: UUID, call_id: UUID, actor: str) -> bool:
    """Delete a call and its recording; False if the call is not in this org.

    The recording goes first: if the database step then fails, a retry finishes the job,
    whereas the reverse order could leave an orphaned recording that nothing points to."""
    with store.connect(settings.database_url) as conn:
        row = store.call_storage(conn, org_id, call_id)
    if row is None:
        return False
    if row["recording_key"] and settings.s3_endpoint:
        recordings.remove(settings, row["recording_key"])
    with store.connect(settings.database_url) as conn:
        deleted = store.delete_call(conn, org_id, call_id)
        if deleted:
            store.audit(conn, org_id, actor, "call.delete", str(call_id))
    return deleted


def purge_expired(settings: Settings) -> int:
    """Delete every call older than the retention period. Returns how many were deleted."""
    if settings.retention_days <= 0:
        return 0
    total = 0
    while True:
        with store.connect(settings.database_url) as conn:
            batch = store.expired_calls(conn, settings.retention_days)
        for call in batch:
            total += delete_call(settings, call["org_id"], call["id"], "retention")
        if len(batch) < 100:
            break
    if total:
        log.info("retention: deleted %d call(s) older than %d days", total, settings.retention_days)
    return total
