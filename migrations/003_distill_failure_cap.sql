-- Migration 003: Distillation failure cap
-- Adds distill_failures counter to imported_sessions.
-- Sessions that fail 3+ times are automatically skipped by distill_sessions.py.
--
-- Run with:
--   docker exec -i claude-memory-db-1 psql -U claude -d memory < migrations/003_distill_failure_cap.sql

BEGIN;

ALTER TABLE imported_sessions ADD COLUMN IF NOT EXISTS distill_failures INT DEFAULT 0;

COMMIT;

-- Report
SELECT
    COUNT(*) FILTER (WHERE distilled = FALSE AND distill_failures < 3)  AS pending,
    COUNT(*) FILTER (WHERE distilled = TRUE)                            AS distilled,
    COUNT(*) FILTER (WHERE distill_failures >= 3)                       AS capped
FROM imported_sessions;
