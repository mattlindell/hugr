-- Migration 002: Soft deletes
-- Adds deleted_at column so memories can be hidden without permanent data loss.
-- Purge requires a separate call to permanently remove a soft-deleted row.
--
-- Run with:
--   docker exec -i claude-memory-db-1 psql -U claude -d memory < migrations/002_soft_deletes.sql

BEGIN;

-- Step 1: Add deleted_at column (NULL = active, non-NULL = soft-deleted)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL;

-- Step 2: Partial index — keeps all active-row queries fast
CREATE INDEX IF NOT EXISTS idx_memories_deleted_at ON memories(deleted_at)
    WHERE deleted_at IS NULL;

COMMIT;

-- Report
SELECT
    COUNT(*) FILTER (WHERE deleted_at IS NULL)     AS active_memories,
    COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) AS deleted_memories
FROM memories;
