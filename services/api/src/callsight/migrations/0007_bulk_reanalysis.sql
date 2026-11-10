-- "Re-analyze all calls": set on each finished call, cleared as each one is re-analyzed.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS reanalyze_requested_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS calls_reanalyze_idx ON calls (org_id, reanalyze_requested_at)
    WHERE reanalyze_requested_at IS NOT NULL;
