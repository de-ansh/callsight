"""API keys, organizations and signed recording links."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID

import psycopg

log = logging.getLogger(__name__)
KEY_PREFIX = "cs_"
RECORDING_LINK_TTL_S = 6 * 3600


@dataclass(frozen=True)
class Principal:
    org_id: UUID
    actor: str  # for the audit log, e.g. "key:laptop"


def hash_key(key: str) -> str:
    # Keys are 256-bit random tokens, so a fast hash is enough (no password-style stretching needed).
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(conn: psycopg.Connection, org_name: str, key_name: str) -> str:
    """Create an API key for an org (creating the org if needed). Returns the key; only its hash is stored."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute("INSERT INTO orgs (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (org_name,))
        org_id = conn.execute("SELECT id FROM orgs WHERE name = %s", (org_name,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO api_keys (org_id, name, key_hash) VALUES (%s, %s, %s)", (org_id, key_name, hash_key(key))
        )
        conn.execute(
            "INSERT INTO audit_log (org_id, actor, action, target) VALUES (%s, 'admin-cli', 'key.create', %s)",
            (org_id, key_name),
        )
    return key


def revoke_key(conn: psycopg.Connection, org_name: str, key_name: str) -> int:
    with conn.transaction():
        return conn.execute(
            "UPDATE api_keys SET revoked_at = now() FROM orgs"
            " WHERE api_keys.org_id = orgs.id AND orgs.name = %s AND api_keys.name = %s AND revoked_at IS NULL",
            (org_name, key_name),
        ).rowcount


def principal_for_key(conn: psycopg.Connection, key: str) -> Principal | None:
    row = conn.execute(
        "SELECT org_id, name FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL", (hash_key(key),)
    ).fetchone()
    return Principal(row["org_id"], f"key:{row['name']}") if row else None


# --- signed recording links: an <audio> element cannot send an Authorization header ----------

_FALLBACK_SECRET = secrets.token_hex(32)


def _link_secret(configured: str) -> bytes:
    if not configured:
        # shortcut: per-process secret, so links break on restart and across API replicas;
        # set CALLSIGHT_SECRET_KEY in any real deployment.
        return _FALLBACK_SECRET.encode()
    return configured.encode()


def _link_sig(call_id: UUID, org_id: UUID, actor: str, expires: int, secret: str) -> str:
    message = f"{call_id}\n{org_id}\n{actor}\n{expires}".encode()
    return hmac.new(_link_secret(secret), message, hashlib.sha256).hexdigest()


def sign_recording(call_id: UUID, org_id: UUID, actor: str, secret: str, now: float | None = None) -> str:
    """A link that plays one call's recording for a few hours. The actor is signed in, so the
    audit log still records who listened."""
    expires = int((time.time() if now is None else now) + RECORDING_LINK_TTL_S)
    query = urlencode({"org": org_id, "by": actor, "expires": expires,
                       "sig": _link_sig(call_id, org_id, actor, expires, secret)})
    return f"/v1/calls/{call_id}/recording?{query}"


def verify_recording(call_id: UUID, org_id: UUID, actor: str, expires: int, sig: str, secret: str) -> bool:
    if expires < time.time():
        return False
    return hmac.compare_digest(_link_sig(call_id, org_id, actor, expires, secret), sig)
