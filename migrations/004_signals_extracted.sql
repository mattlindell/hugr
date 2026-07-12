-- Migration 004: Behavioral signal extraction tracking
-- Adds signals_extracted flag to imported_sessions so extract_signals.py
-- knows which sessions have already been processed.
--
-- Run with:
--   docker exec -i claude-memory-db psql -U claude -d memory < migrations/004_signals_extracted.sql
--
-- Note: extract_signals.py also applies this migration automatically at startup
-- via ALTER TABLE ... ADD COLUMN IF NOT EXISTS.

BEGIN;

ALTER TABLE imported_sessions
    ADD COLUMN IF NOT EXISTS signals_extracted BOOLEAN DEFAULT FALSE;

COMMIT;

-- Report
SELECT
    COUNT(*) FILTER (WHERE signals_extracted = FALSE) AS pending_extraction,
    COUNT(*) FILTER (WHERE signals_extracted = TRUE)  AS already_extracted,
    COUNT(*)                                           AS total_sessions
FROM imported_sessions;
