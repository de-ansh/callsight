-- Semantic search: one vector per transcript line per embedding model. Rows go when their segment
-- goes (delete, retention, re-analysis), and the worker fills them back in.
CREATE TABLE IF NOT EXISTS segment_embeddings (
    call_id   UUID NOT NULL,
    idx       INTEGER NOT NULL,
    model     TEXT NOT NULL,
    embedding vector NOT NULL,  -- no fixed size, so switching models needs no migration
    PRIMARY KEY (call_id, idx, model),
    FOREIGN KEY (call_id, idx) REFERENCES segments (call_id, idx) ON DELETE CASCADE
);
