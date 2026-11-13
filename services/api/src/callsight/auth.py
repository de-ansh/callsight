"""API keys, organizations and signed recording links."""

from __future__ import annotations

import hashlib
import re
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)
KEY_PREFIX = "cs_"
RECORDING_LINK_TTL_S = 6 * 3600


@dataclass(frozen=True)
class Principal:
    org_id: UUID
    actor: str  # for the audit log, e.g. "key:laptop"
    admin: bool = False  # may manage keys, domains and admins from the UI


def hash_key(key: str) -> str:
    # Keys are 256-bit random tokens, so a fast hash is enough (no password-style stretching needed).
    return hashlib.sha256(key.encode()).hexdigest()


def _org_id(conn: psycopg.Connection, org_name: str) -> UUID:
    conn.execute("INSERT INTO orgs (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (org_name,))
    return conn.execute("SELECT id FROM orgs WHERE name = %s", (org_name,)).fetchone()["id"]


def add_key(conn: psycopg.Connection, org_id: UUID, key_name: str, admin: bool, actor: str) -> tuple[str, UUID]:
    """Create an API key in an org. Returns (key, id); only the key's hash is stored."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    with conn.transaction():
        key_id = conn.execute(
            "INSERT INTO api_keys (org_id, name, key_hash, admin) VALUES (%s, %s, %s, %s) RETURNING id",
            (org_id, key_name, hash_key(key), admin),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO audit_log (org_id, actor, action, target, details) VALUES (%s, %s, 'key.create', %s, %s)",
            (org_id, actor, key_name, Jsonb({"admin": admin})),
        )
    return key, key_id


def create_key(conn: psycopg.Connection, org_name: str, key_name: str, admin: bool = False) -> str:
    """Create an API key for an org (creating the org if needed). Returns the key; only its hash is stored."""
    with conn.transaction():
        return add_key(conn, _org_id(conn, org_name), key_name, admin, "admin-cli")[0]


def revoke_key_id(conn: psycopg.Connection, org_id: UUID, key_id: UUID, actor: str) -> str | None:
    """Revoke one active key in this org. Returns its name, or None if there is no such active key."""
    with conn.transaction():
        row = conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND org_id = %s AND revoked_at IS NULL RETURNING name",
            (key_id, org_id),
        ).fetchone()
        if row:
            conn.execute(
                "INSERT INTO audit_log (org_id, actor, action, target) VALUES (%s, %s, 'key.revoke', %s)",
                (org_id, actor, row["name"]),
            )
    return row["name"] if row else None


def revoke_key(conn: psycopg.Connection, org_name: str, key_name: str) -> int:
    with conn.transaction():
        return conn.execute(
            "UPDATE api_keys SET revoked_at = now() FROM orgs"
            " WHERE api_keys.org_id = orgs.id AND orgs.name = %s AND api_keys.name = %s AND revoked_at IS NULL",
            (org_name, key_name),
        ).rowcount


def principal_for_key(conn: psycopg.Connection, key: str) -> Principal | None:
    row = conn.execute(
        "SELECT org_id, name, admin FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL", (hash_key(key),)
    ).fetchone()
    return Principal(row["org_id"], f"key:{row['name']}", row["admin"]) if row else None


# --- single sign-on: email domain -> organization -----------------------------------------

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalize_domain(domain: str) -> str:
    domain = domain.strip().lower().lstrip("@")
    if not _DOMAIN.match(domain):
        raise ValueError(f"not a valid domain: {domain!r}")
    return domain


def allow_domain(conn: psycopg.Connection, org_name: str, domain: str) -> None:
    """People who sign in with a verified email at `domain` join `org_name` (created if needed)."""
    domain = normalize_domain(domain)
    with conn.transaction():
        conn.execute("INSERT INTO orgs (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (org_name,))
        org_id = conn.execute("SELECT id FROM orgs WHERE name = %s", (org_name,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO org_domains (domain, org_id) VALUES (%s, %s)"
            " ON CONFLICT (domain) DO UPDATE SET org_id = EXCLUDED.org_id",
            (domain, org_id),
        )
        conn.execute(
            "INSERT INTO audit_log (org_id, actor, action, target) VALUES (%s, 'admin-cli', 'domain.allow', %s)",
            (org_id, domain),
        )


def disallow_domain(conn: psycopg.Connection, domain: str) -> int:
    with conn.transaction():
        return conn.execute("DELETE FROM org_domains WHERE domain = %s", (normalize_domain(domain),)).rowcount


def remove_org_domain(conn: psycopg.Connection, org_id: UUID, domain: str, actor: str) -> bool:
    """Stop sign-in for one of this org's domains (its sessions end at once). False if it isn't this org's."""
    try:
        domain = normalize_domain(domain)
    except ValueError:
        return False
    with conn.transaction():
        removed = conn.execute("DELETE FROM org_domains WHERE domain = %s AND org_id = %s", (domain, org_id)).rowcount
        if removed:
            conn.execute(
                "INSERT INTO audit_log (org_id, actor, action, target) VALUES (%s, %s, 'domain.disallow', %s)",
                (org_id, actor, domain),
            )
    return bool(removed)


# --- admins who sign in with SSO ------------------------------------------------------------

def normalize_email(email: str) -> str:
    local, at, domain = email.strip().lower().rpartition("@")
    if not at or not local or len(email) > 254 or any(c.isspace() for c in local):
        raise ValueError(f"not a valid email: {email!r}")
    return f"{local}@{normalize_domain(domain)}"


def set_email_admin(conn: psycopg.Connection, org_id: UUID, email: str, admin: bool, actor: str) -> bool:
    """Grant or remove admin for a signed-in email. Returns whether anything changed."""
    email = normalize_email(email)
    with conn.transaction():
        if admin:
            changed = conn.execute(
                "INSERT INTO org_admins (org_id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING", (org_id, email)
            ).rowcount
        else:
            changed = conn.execute("DELETE FROM org_admins WHERE org_id = %s AND email = %s", (org_id, email)).rowcount
        if changed:
            conn.execute(
                "INSERT INTO audit_log (org_id, actor, action, target) VALUES (%s, %s, %s, %s)",
                (org_id, actor, "admin.grant" if admin else "admin.revoke", email),
            )
    return bool(changed)


def is_email_admin(conn: psycopg.Connection, org_id: UUID, email: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM org_admins WHERE org_id = %s AND email = %s", (org_id, email.strip().lower())
    ).fetchone() is not None


def org_for_email(conn: psycopg.Connection, email: str) -> UUID | None:
    """The organization a verified email belongs to, by its domain; None if the domain is not allowed."""
    _, _, domain = email.strip().lower().rpartition("@")
    if not domain:
        return None
    row = conn.execute("SELECT org_id FROM org_domains WHERE domain = %s", (domain,)).fetchone()
    return row["org_id"] if row else None


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
