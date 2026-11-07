-- Tenancy, API keys and an audit log. Existing calls move into a 'default' org.

CREATE TABLE IF NOT EXISTS orgs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name           TEXT NOT NULL UNIQUE,
    -- HMAC secret for this org's webhooks. gen_random_uuid() is from a cryptographic RNG.
    webhook_secret TEXT NOT NULL DEFAULT encode(sha256(gen_random_uuid()::text::bytea), 'hex'),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,   -- sha256 of the key; the key itself is shown once
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);

INSERT INTO orgs (name) VALUES ('default') ON CONFLICT (name) DO NOTHING;

ALTER TABLE calls ADD COLUMN IF NOT EXISTS org_id UUID REFERENCES orgs(id) ON DELETE CASCADE;
UPDATE calls SET org_id = (SELECT id FROM orgs WHERE name = 'default') WHERE org_id IS NULL;
ALTER TABLE calls ALTER COLUMN org_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS calls_org_created_idx ON calls (org_id, created_at DESC, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id       BIGSERIAL PRIMARY KEY,
    org_id   UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    actor    TEXT NOT NULL,      -- "key:<name>", "webhook:<connector>", "admin-cli"
    action   TEXT NOT NULL,      -- e.g. call.create, call.read, recording.read, key.create
    target   TEXT,
    details  JSONB NOT NULL DEFAULT '{}',
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_org_at_idx ON audit_log (org_id, at DESC);
