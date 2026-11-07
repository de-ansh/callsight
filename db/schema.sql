-- Schema. Mounted into Postgres on first start only (docker-entrypoint-initdb.d).
-- shortcut: no migrations tool; edits here need `docker compose down -v` to apply. Add one before the first real deploy.

CREATE EXTENSION IF NOT EXISTS vector;  -- reserved for semantic search over transcripts

CREATE TABLE IF NOT EXISTS calls (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id TEXT,
    source      TEXT NOT NULL DEFAULT 'upload',
    duration_s  DOUBLE PRECISION,
    summary     TEXT NOT NULL DEFAULT '',
    talk_share  JSONB NOT NULL DEFAULT '{}',
    -- Audio jobs: queued -> processing -> done | failed. Text transcripts are stored as done.
    status        TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('queued', 'processing', 'done', 'failed')),
    recording_key TEXT,          -- object key in the recordings bucket
    error         TEXT,
    claimed_at    TIMESTAMPTZ,   -- when a worker took the job; stale claims are retried
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS calls_pending_idx ON calls (created_at) WHERE status IN ('queued', 'processing');

CREATE TABLE IF NOT EXISTS segments (
    call_id  UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,
    start_s  DOUBLE PRECISION NOT NULL,
    speaker  TEXT NOT NULL,
    text     TEXT NOT NULL,
    PRIMARY KEY (call_id, idx)
);

CREATE TABLE IF NOT EXISTS insights (
    call_id   UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    severity  TEXT NOT NULL,
    title     TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    evidence  JSONB NOT NULL DEFAULT '[]',
    PRIMARY KEY (call_id, idx)
);

CREATE INDEX IF NOT EXISTS calls_created_idx ON calls (created_at DESC, id);
