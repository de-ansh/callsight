-- Admin roles. Admins manage API keys, remove sign-in domains and name other admins from the UI.
-- Existing keys stay members: management was CLI-only before, so nobody loses a right.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS admin BOOLEAN NOT NULL DEFAULT false;

-- Signed-in (SSO) people who are admins of an organization.
CREATE TABLE IF NOT EXISTS org_admins (
    org_id     UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email      TEXT NOT NULL CHECK (email = lower(email)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, email)
);
