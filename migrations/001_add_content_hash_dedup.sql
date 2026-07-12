-- Migration 001: Add content_hash for exact-duplicate prevention
-- Deduplicates existing data, then enforces uniqueness going forward.
--
-- Run with:
--   docker exec -i claude-memory-db-1 psql -U claude -d memory < migrations/001_add_content_hash_dedup.sql

BEGIN;

-- Step 1: Add content_hash as a generated column (md5 of content)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_hash TEXT
    GENERATED ALWAYS AS (md5(content)) STORED;

-- Step 2: Delete exact duplicates, keeping the earliest (lowest id) per content
DELETE FROM memories
WHERE id NOT IN (
    SELECT MIN(id)
    FROM memories
    GROUP BY content_hash
);

-- Step 3: Enforce uniqueness going forward
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash);

COMMIT;

-- Report
SELECT COUNT(*) AS remaining_memories FROM memories;
