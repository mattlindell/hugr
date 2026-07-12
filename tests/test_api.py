"""Tests for REST API helper functions added to server.py."""
import sys, os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-server"))
import server


def _make_cur(rows):
    """Return a mock cursor whose fetchall() returns rows."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    return cur


def _make_conn(cur):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    return conn


class TestApiProjects:
    def test_returns_list_of_project_dicts(self):
        cur = _make_cur([{"project": "workspace", "count": 42},
                         {"project": "claude-memory", "count": 7}])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_projects()
        assert result == [{"project": "workspace", "count": 42},
                          {"project": "claude-memory", "count": 7}]

    def test_empty_db_returns_empty_list(self):
        cur = _make_cur([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_projects()
        assert result == []


class TestApiTags:
    def test_returns_tag_counts(self):
        cur = _make_cur([{"tag": "type:decision", "count": 15}])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_tags()
        assert result == [{"tag": "type:decision", "count": 15}]

    def test_empty_db_returns_empty_list(self):
        cur = _make_cur([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_tags()
        assert result == []


class TestApiStats:
    def test_returns_stats_dict(self):
        cur = _make_cur([{"active": 247, "deleted": 3, "projects": 12,
                          "avg_content_len": 512}])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_stats()
        assert result["active"] == 247
        assert result["projects"] == 12
        assert "storage_mb" in result
        assert result["storage_mb"] == 0.9
        assert result["storage_breakdown"]["embeddings_mb"] == 0.7
        assert result["storage_breakdown"]["content_mb"] == 0.1
        assert result["storage_breakdown"]["metadata_mb"] == 0.0


class TestApiListMemories:
    def test_returns_memory_list(self):
        row = {"id": 1, "content": "test content", "tags": ["type:decision"],
               "source": "claude-code", "project": "workspace",
               "created_at": "2026-05-01T10:00:00", "updated_at": "2026-05-01T10:00:00"}
        cur = _make_cur([row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_list_memories()
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["title"] == "test content"  # no truncation needed here

    def test_truncates_long_content_to_title(self):
        long = "A" * 100
        row = {"id": 2, "content": long, "tags": [], "source": "claude-code",
               "project": "", "created_at": "2026-05-01T10:00:00",
               "updated_at": "2026-05-01T10:00:00"}
        cur = _make_cur([row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_list_memories()
        assert result[0]["title"].endswith("…")
        assert len(result[0]["title"]) <= 73  # 72 chars + ellipsis

    def test_raises_on_invalid_date(self):
        import pytest
        with pytest.raises(ValueError):
            server._api_list_memories(since="not-a-date")


class TestApiGetMemory:
    def test_returns_memory_dict(self):
        row = {"id": 5, "content": "hello", "tags": ["type:fix"],
               "source": "claude-code", "project": "workspace",
               "created_at": "2026-04-30T09:00:00", "updated_at": "2026-04-30T09:00:00",
               "deleted_at": None}
        cur = _make_cur([row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_get_memory(5)
        assert result["id"] == 5
        assert result["content"] == "hello"

    def test_returns_none_when_not_found(self):
        cur = _make_cur([])
        cur.fetchone.return_value = None
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_get_memory(999)
        assert result is None


class TestApiRelatedMemories:
    def test_returns_related_list(self):
        source_row = {"id": 1, "content": "source memory content", "tags": [], "project": "workspace",
                      "created_at": "2026-04-30T10:00:00", "updated_at": "2026-04-30T10:00:00",
                      "deleted_at": None}
        related_row = {"id": 3, "content": "related memory", "tags": [], "project": "workspace",
                       "created_at": "2026-04-28T08:00:00", "sim": 0.88}
        # First DB call (get_memory): fetchone returns source_row
        # Second DB call (related query): fetchall returns [related_row]
        cur_source = _make_cur([source_row])
        cur_related = _make_cur([related_row])
        call_count = [0]
        def db_conn_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_conn(cur_source)
            return _make_conn(cur_related)
        with patch("server.db_conn") as mock_db, \
             patch("server.embed", return_value=[0.0] * 768):
            mock_db.return_value = MagicMock()
            mock_db.side_effect = db_conn_side_effect
            result = server._api_related_memories(1, limit=3)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 3
        assert isinstance(result[0]["sim"], float)
        assert "title" in result[0]

    def test_returns_empty_when_memory_not_found(self):
        cur = _make_cur([])
        cur.fetchone.return_value = None
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_related_memories(999, limit=3)
        assert result == []


class TestApiRecall:
    def test_returns_ranked_results(self):
        row = {"id": 7, "content": "auth middleware is driven by compliance",
               "tags": ["type:decision"], "project": "workspace",
               "created_at": "2026-04-30T10:00:00", "sim": 0.91}
        cur = _make_cur([row])
        with patch("server.db_conn") as mock_db, \
             patch("server.embed", return_value=[0.0] * 768):
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_recall("auth compliance", threshold=0.78)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 7
        assert isinstance(result[0]["sim"], float)
        assert "title" in result[0]
        assert "snippet" in result[0]

    def test_empty_query_returns_empty_list(self):
        result = server._api_recall("", threshold=0.78)
        assert result == []

    def test_whitespace_only_query_returns_empty_list(self):
        result = server._api_recall("   ", threshold=0.78)
        assert result == []


class TestApiPreferences:
    def test_groups_by_category_tag(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        rows = [
            {"id": 1, "content": "Prefers terse responses", "tags": ["type:preference", "category:workflow"],
             "project": "workspace", "created_at": recent, "updated_at": recent},
            {"id": 2, "content": "Uses brew Python", "tags": ["type:preference", "category:stack"],
             "project": "workspace", "created_at": recent, "updated_at": recent},
        ]
        cur = _make_cur(rows)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_preferences()
        categories = {g["category"] for g in result}
        assert "workflow" in categories
        assert "stack" in categories

    def test_confidence_high_for_recent_memories(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        rows = [{"id": 1, "content": "Recent pref", "tags": ["type:preference"],
                 "project": "", "created_at": recent, "updated_at": recent}]
        cur = _make_cur(rows)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_preferences()
        assert result[0]["items"][0]["confidence"] >= 0.9

    def test_confidence_low_for_old_memories(self):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        rows = [{"id": 2, "content": "Old pref", "tags": ["type:preference"],
                 "project": "", "created_at": old, "updated_at": old}]
        cur = _make_cur(rows)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_preferences()
        assert result[0]["items"][0]["confidence"] == 0.65

    def test_no_preferences_returns_empty_list(self):
        cur = _make_cur([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_preferences()
        assert result == []

    def test_confidence_medium_for_moderately_old_memories(self):
        from datetime import datetime, timezone, timedelta
        mid = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        rows = [{"id": 3, "content": "Medium-age pref", "tags": ["type:preference"],
                 "project": "", "created_at": mid, "updated_at": mid}]
        cur = _make_cur(rows)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_preferences()
        assert result[0]["items"][0]["confidence"] == 0.80


class TestApiBulkDelete:
    def test_dry_run_returns_count_without_deleting(self):
        cur = _make_cur([])
        cur.fetchone.return_value = (5,)  # COUNT(*) returns a tuple
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=_make_conn(cur))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_bulk_delete(project="workspace", tag=None, dry_run=True)
        assert result["dry_run"] is True
        assert result["deleted"] == 5

    def test_requires_at_least_one_filter(self):
        result = server._api_bulk_delete(project=None, tag=None, dry_run=False)
        assert "error" in result

    def test_live_run_returns_deleted_count(self):
        cur = _make_cur([])
        cur.rowcount = 3
        conn = _make_conn(cur)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server._api_bulk_delete(project="workspace", tag=None, dry_run=False)
        assert result["dry_run"] is False
        assert result["deleted"] == 3
