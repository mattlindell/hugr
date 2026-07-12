"""Tests for distill_sessions.py — pure-logic functions only (no DB, API, or model required)."""
import sys
import os
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import distill_sessions as ds


class TestParseDistilled:
    def test_valid_json_array(self):
        response = '[{"content": "Use brew for Python packages.", "tags": ["brew", "python"]}]'
        result = ds.parse_distilled(response)
        assert len(result) == 1
        assert result[0]["content"] == "Use brew for Python packages."
        assert result[0]["tags"] == ["brew", "python"]

    def test_json_with_preamble(self):
        response = 'Here are the memories:\n[{"content": "foo", "tags": ["bar"]}]'
        result = ds.parse_distilled(response)
        assert len(result) == 1
        assert result[0]["content"] == "foo"

    def test_empty_array(self):
        result = ds.parse_distilled("[]")
        assert result == []

    def test_no_array_returns_empty(self):
        result = ds.parse_distilled("Nothing useful was learned.")
        assert result == []

    def test_multiple_items(self):
        response = '[{"content": "a", "tags": []}, {"content": "b", "tags": ["x"]}]'
        result = ds.parse_distilled(response)
        assert len(result) == 2
        assert result[1]["content"] == "b"

    def test_whitespace_only(self):
        result = ds.parse_distilled("   ")
        assert result == []


class TestBuildTranscript:
    def _msg(self, text):
        return {"content": text}

    def test_joins_messages_with_separator(self):
        messages = [self._msg("hello"), self._msg("world")]
        transcript = ds.build_transcript(messages)
        assert "hello" in transcript
        assert "world" in transcript
        assert "---" in transcript

    def test_skips_empty_content(self):
        messages = [self._msg("  "), self._msg("kept")]
        transcript = ds.build_transcript(messages)
        assert transcript == "kept"

    def test_truncates_long_transcript(self):
        long_text = "x" * (ds.MAX_TRANSCRIPT_CHARS + 1000)
        messages = [self._msg(long_text)]
        transcript = ds.build_transcript(messages)
        assert len(transcript) <= ds.MAX_TRANSCRIPT_CHARS + 100
        assert "[transcript truncated]" in transcript

    def test_short_transcript_not_truncated(self):
        messages = [self._msg("short message")]
        transcript = ds.build_transcript(messages)
        assert "[transcript truncated]" not in transcript


class TestIncrementFailures:
    def _make_conn(self, failures_after=1):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = (failures_after,)
        conn.cursor.return_value = cur
        return conn, cur

    def test_increments_counter(self):
        conn, cur = self._make_conn(failures_after=1)
        ds._increment_failures(conn, "session-abc", "session-a")
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "distill_failures" in sql
        assert "distill_failures + 1" in sql
        conn.commit.assert_called_once()

    def test_warns_when_cap_reached(self):
        conn, cur = self._make_conn(failures_after=ds.DISTILL_FAILURE_CAP)
        with patch.object(ds.log, "warning") as mock_warn:
            ds._increment_failures(conn, "session-abc", "session-a")
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        assert "cap" in msg.lower() or "skip" in msg.lower()

    def test_no_warning_below_cap(self):
        conn, cur = self._make_conn(failures_after=ds.DISTILL_FAILURE_CAP - 1)
        with patch.object(ds.log, "warning") as mock_warn:
            ds._increment_failures(conn, "session-abc", "session-a")
        mock_warn.assert_not_called()

    def test_handles_db_error_gracefully(self):
        conn = MagicMock()
        conn.cursor.side_effect = Exception("db error")
        # Should not raise
        ds._increment_failures(conn, "session-abc", "session-a")
        conn.rollback.assert_called_once()


class TestGetPendingSessionsFiltersCapped:
    def test_query_excludes_capped_sessions(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        ds.get_pending_sessions(conn)
        sql = cur.execute.call_args[0][0]
        assert "distill_failures" in sql
        assert "distilled = FALSE" in sql

    def test_query_filters_capped_with_project(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        ds.get_pending_sessions(conn, project_filter="myproject")
        sql = cur.execute.call_args[0][0]
        assert "distill_failures" in sql
        assert "ILIKE" in sql

    def test_query_excludes_short_sessions(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        ds.get_pending_sessions(conn)
        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "message_count" in sql
        assert ds.MIN_MESSAGE_COUNT in params

    def test_query_excludes_short_sessions_with_project(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        ds.get_pending_sessions(conn, project_filter="myproject")
        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "message_count" in sql
        assert ds.MIN_MESSAGE_COUNT in params


class TestFilterNearDupes:
    def _make_conn(self, sim):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = {"id": 1, "content": "existing memory", "sim": sim}
        conn.cursor.return_value = cur
        return conn

    def test_drops_near_duplicate(self):
        conn = self._make_conn(sim=0.90)
        contents, vectors = ds.filter_near_dupes(conn, ["new memory"], [[0.1, 0.2]], "abc123")
        assert contents == []
        assert vectors == []

    def test_keeps_distinct_memory(self):
        conn = self._make_conn(sim=0.50)
        contents, vectors = ds.filter_near_dupes(conn, ["new memory"], [[0.1, 0.2]], "abc123")
        assert contents == ["new memory"]

    def test_keeps_at_threshold_boundary(self):
        # Exactly at threshold — should be dropped (>=)
        conn = self._make_conn(sim=ds.DISTILL_DEDUP_THRESHOLD)
        contents, vectors = ds.filter_near_dupes(conn, ["new memory"], [[0.1, 0.2]], "abc123")
        assert contents == []

    def test_no_existing_memories(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None
        conn.cursor.return_value = cur
        contents, vectors = ds.filter_near_dupes(conn, ["new memory"], [[0.1, 0.2]], "abc123")
        assert contents == ["new memory"]

    def test_mixed_keeps_and_drops(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.side_effect = [
            {"id": 1, "content": "existing", "sim": 0.95},  # drop
            {"id": 2, "content": "other",    "sim": 0.40},  # keep
        ]
        conn.cursor.return_value = cur
        contents, vectors = ds.filter_near_dupes(
            conn, ["memory a", "memory b"], [[0.1], [0.2]], "abc123"
        )
        assert contents == ["memory b"]
