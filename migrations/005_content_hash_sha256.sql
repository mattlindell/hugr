-- Migration 005: Upgrade content_hash from md5 to SHA-256 (pgcrypto)
-- PR #35 updated init.sql to use encode(digest(content,'sha256'),'hex') instead of md5().
-- Existing deployments that ran migration 001 have an md5-based content_hash column.
-- This migration drops and recreates the column and its unique index using SHA-256.
--
-- Safe to run on a fresh DB — IF NOT EXISTS guards make it idempotent.
--
-- Run with:
--   docker exec -i claude-memory-db psql -U claude -d memory < migrations/005_content_hash_sha256.sql

BEGIN;

-- pgcrypto provides digest() for SHA-256
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Drop the old md5-based column and its unique index (index drops with the column).
-- IF EXISTS makes this safe on fresh DBs that got SHA-256 from init.sql directly.
ALTER TABLE memories DROP COLUMN IF EXISTS content_hash;

-- Recreate with SHA-256 (64-char hex), matching current init.sql definition.
ALTER TABLE memories
    ADD COLUMN content_hash TEXT
    GENERATED ALWAYS AS (encode(digest(content, 'sha256'), 'hex')) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash);

COMMIT;

-- Report
SELECT
    COUNT(*)                                      AS total_memories,
    COUNT(DISTINCT content_hash)                  AS unique_content_hashes,
    COUNT(*) - COUNT(DISTINCT content_hash)       AS unexpected_duplicates
FROM memories;
