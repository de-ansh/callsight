-- Webhook connectors can hand over a recording by URL; the worker fetches it into storage.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_url TEXT;
