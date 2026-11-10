-- Names given at upload for a stereo recording's channels, e.g. {"Speaker 1": "Alex", "Speaker 2": "Priya"}.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS speaker_names JSONB;
