-- Which speaker is the sales rep on this call, when someone has said so (Speakers panel).
-- NULL = guess (CALLSIGHT_REP_NAMES, else whoever speaks first).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS rep_speaker TEXT;
