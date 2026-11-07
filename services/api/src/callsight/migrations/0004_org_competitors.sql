-- Competitor names are per organization (edited on the Settings page), not one global list.
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS competitors TEXT[] NOT NULL DEFAULT '{}';
