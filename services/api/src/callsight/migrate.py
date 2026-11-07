"""Schema migrations: numbered .sql files in callsight/migrations, applied in order, once each.

The API and the worker both call migrate() at startup; an advisory lock makes that safe when
several processes start together. Run by hand with: python -m callsight.migrate
"""

from __future__ import annotations

import logging
from importlib.resources import files

import psycopg

log = logging.getLogger("callsight.migrate")
_LOCK_ID = 7_201_964  # arbitrary, constant: identifies callsight's migration lock


def pending_files() -> list[tuple[str, str]]:
    """(version, sql) for every bundled migration, in order. Version = file name without .sql."""
    found = [f for f in files("callsight.migrations").iterdir() if f.name.endswith(".sql")]
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in sorted(found, key=lambda f: f.name)]


def migrate(database_url: str) -> list[str]:
    """Apply migrations not yet recorded in schema_migrations; returns the versions applied."""
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations"
                " (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in pending_files():
                if version in done:
                    continue
                with conn.transaction():  # a failing migration leaves no partial schema behind
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                log.info("applied migration %s", version)
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied_now


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO)
    url = os.environ.get("CALLSIGHT_DATABASE_URL")
    if not url:
        raise SystemExit("set CALLSIGHT_DATABASE_URL")
    print("applied:", migrate(url) or "nothing (up to date)")
