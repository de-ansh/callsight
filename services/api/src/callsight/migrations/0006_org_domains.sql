-- Single sign-on: people whose verified email is at one of these domains join that organization.
CREATE TABLE IF NOT EXISTS org_domains (
    domain     TEXT PRIMARY KEY CHECK (domain = lower(domain)),
    org_id     UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
