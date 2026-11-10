-- Trigram indexes so "search call names and what was said" (ILIKE '%term%') stays fast as calls pile up.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS segments_text_trgm_idx ON segments USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS calls_external_id_trgm_idx ON calls USING gin (external_id gin_trgm_ops);
