CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE memories (
  id           SERIAL PRIMARY KEY,
  content      TEXT         NOT NULL,
  content_hash TEXT         GENERATED ALWAYS AS (encode(digest(content, 'sha256'), 'hex')) STORED,
  tags         TEXT[]       DEFAULT '{}',
  source       VARCHAR(100) DEFAULT 'claude-code',
  project      VARCHAR(100) DEFAULT '',
  embedding    vector(768),
  created_at   TIMESTAMP    DEFAULT NOW(),
  updated_at   TIMESTAMP    DEFAULT NOW(),
  deleted_at   TIMESTAMP    DEFAULT NULL
);

CREATE UNIQUE INDEX idx_memories_content_hash ON memories(content_hash);
CREATE INDEX idx_memories_tags      ON memories USING GIN(tags);
CREATE INDEX idx_memories_created   ON memories(created_at DESC);
CREATE INDEX idx_memories_project   ON memories(project);
CREATE INDEX idx_memories_fts       ON memories USING GIN(to_tsvector('english', content));
CREATE INDEX idx_memories_embedding ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_memories_deleted_at ON memories(deleted_at) WHERE deleted_at IS NULL;

CREATE TABLE imported_sessions (
  session_id       VARCHAR(100) PRIMARY KEY,
  project          VARCHAR(100) DEFAULT '',
  imported_at      TIMESTAMP    DEFAULT NOW(),
  message_count    INT          DEFAULT 0,
  distilled        BOOLEAN      DEFAULT FALSE,
  distill_failures INT          DEFAULT 0
);

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at
BEFORE UPDATE ON memories
FOR EACH ROW EXECUTE FUNCTION update_updated_at();
