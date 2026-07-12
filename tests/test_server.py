"""Tests for mcp-server/server.py — pure-logic and tool functions (DB mocked)."""
import sys
import os
import json
from datetime import datetime
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-server"))

import server


class TestParseDt:
    def test_valid_date(self):
        dt, err = server._parse_dt("2026-01-15", "since")
        assert dt == datetime(2026, 1, 15)
        assert err is None

    def test_valid_datetime(self):
        dt, err = server._parse_dt("2026-01-15T12:30:00", "before")
        assert dt == datetime(2026, 1, 15, 12, 30, 0)
        assert err is None

    def test_empty_string_returns_none(self):
        dt, err = server._parse_dt("", "since")
        assert dt is None
        assert err is None

    def test_none_returns_none(self):
        dt, err = server._parse_dt(None, "since")
        assert dt is None
        assert err is None

    def test_invalid_date_returns_error_string(self):
        dt, err = server._parse_dt("not-a-date", "since")
        assert dt is None
        assert "❌" in err
        assert "since" in err
        assert "not-a-date" in err


class TestCheckMemory:
    def _make_conn(self, row):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = row
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    def test_add_when_no_memories(self):
        conn = self._make_conn(None)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.check_memory("something new")
        data = json.loads(result)
        assert data["action"] == "ADD"

    def test_noop_when_duplicate(self):
        conn = self._make_conn({"id": 1, "content": "existing", "sim": 0.95})
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.check_memory("existing content")
        data = json.loads(result)
        assert data["action"] == "NOOP"
        assert data["target_id"] == 1

    def test_update_when_similar(self):
        conn = self._make_conn({"id": 2, "content": "similar memory", "sim": 0.80})
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.check_memory("similar content")
        data = json.loads(result)
        assert data["action"] == "UPDATE"
        assert data["target_id"] == 2

    def test_add_when_below_thresholds(self):
        conn = self._make_conn({"id": 3, "content": "unrelated", "sim": 0.50})
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.check_memory("new topic")
        data = json.loads(result)
        assert data["action"] == "ADD"


class TestSaveMemory:
    def _make_conn(self, dup_row=None, insert_row=None):
        """Build a mock connection that simulates DB interactions."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [dup_row, insert_row]
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_duplicate_detected(self):
        dup = {"id": 5, "sim": 0.97, "content": "existing memory content here"}
        conn, cur = self._make_conn(dup_row=dup)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.save_memory("existing memory content here")
        assert "near-duplicate" in result
        assert "5" in result

    def test_successful_save(self):
        insert_row = {"id": 42, "created_at": datetime(2026, 1, 1)}
        conn, cur = self._make_conn(dup_row=None, insert_row=insert_row)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.save_memory("brand new memory")
        assert "✅" in result
        assert "42" in result


class TestSemanticSearch:
    def test_no_results_returns_message(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.semantic_search("something obscure")
        assert "No similar memories" in result

    def test_invalid_since_date_returns_error(self):
        result = server.semantic_search("query", since="bad-date")
        assert "❌" in result

    def test_invalid_before_date_returns_error(self):
        result = server.semantic_search("query", before="not-a-date")
        assert "❌" in result

    def test_results_returned_as_json(self):
        row = {"id": 1, "content": "test", "tags": [], "source": "x", "project": "", "created_at": datetime(2026, 1, 1), "similarity": 0.85}
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [row]
        conn.cursor.return_value = cur
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.semantic_search("test query")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["content"] == "test"


class TestListMemories:
    def _make_conn(self, total=0, rows=None):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        # fetchone returns total count, fetchall returns page rows
        cur.fetchone.return_value = {"count": total}
        cur.fetchall.return_value = rows or []
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    def test_empty_returns_placeholder(self):
        conn = self._make_conn(total=0, rows=[])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.list_memories()
        assert "No memories" in result

    def test_returns_total_and_memories(self):
        row = {"id": 1, "content": "hello", "tags": [], "source": "x",
               "project": "", "created_at": datetime(2026, 1, 1)}
        conn = self._make_conn(total=1, rows=[row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.list_memories()
        data = json.loads(result)
        assert data["total"] == 1
        assert len(data["memories"]) == 1
        assert data["memories"][0]["content"] == "hello"
        assert "offset" in data
        assert "limit" in data

    def test_pagination_offset_param(self):
        conn = self._make_conn(total=50, rows=[])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.list_memories(limit=10, offset=20)
        data = json.loads(result)
        assert data["offset"] == 20
        assert data["limit"] == 10
        assert data["total"] == 50

    def test_invalid_since_returns_error(self):
        result = server.list_memories(since="2026-99-99")
        assert "❌" in result


class TestDeleteMemory:
    def test_delete_existing(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.rowcount = 1
        conn.cursor.return_value = cur
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.delete_memory(7)
        assert "✅" in result
        assert "7" in result

    def test_delete_nonexistent(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.rowcount = 0
        conn.cursor.return_value = cur
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.delete_memory(999)
        assert "❌" in result


class TestExportMemories:
    def test_no_results_returns_message(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.export_memories()
        assert "No memories" in result

    def test_json_export_structure(self):
        row = {"id": 1, "content": "memo", "tags": ["t1"], "source": "s", "project": "p",
               "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1)}
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [row]
        conn.cursor.return_value = cur
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.export_memories(output_format="json")
        data = json.loads(result)
        assert data["count"] == 1
        assert data["memories"][0]["content"] == "memo"

    def test_markdown_export_contains_content(self):
        row = {"id": 2, "content": "markdown memory", "tags": [], "source": "s", "project": "",
               "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1)}
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [row]
        conn.cursor.return_value = cur
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.export_memories(output_format="markdown")
        assert "markdown memory" in result
        assert "# Memory Export" in result

    def test_invalid_format_returns_error(self):
        result = server.export_memories(output_format="csv")
        assert "❌" in result

    def test_invalid_since_returns_error(self):
        result = server.export_memories(since="bad")
        assert "❌" in result


class TestHybridSearch:
    def _make_conn(self, rows):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rows
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    def test_weights_must_sum_to_one(self):
        result = server.hybrid_search("query", keyword_weight=0.6, semantic_weight=0.6)
        assert "❌" in result
        assert "1.0" in result

    def test_negative_weight_rejected(self):
        result = server.hybrid_search("query", keyword_weight=-0.1, semantic_weight=1.1)
        assert "❌" in result

    def test_invalid_since_returns_error(self):
        result = server.hybrid_search("query", since="not-a-date")
        assert "❌" in result

    def test_invalid_before_returns_error(self):
        result = server.hybrid_search("query", before="not-a-date")
        assert "❌" in result

    def test_no_results_returns_message(self):
        conn = self._make_conn([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.hybrid_search("obscure query")
        assert "No memories found" in result

    def test_results_returned_as_json(self):
        row = {
            "id": 1, "content": "test memory", "tags": [], "source": "x",
            "project": "", "created_at": datetime(2026, 1, 1),
            "keyword_score": 0.5, "semantic_score": 0.8, "hybrid_score": 0.59,
        }
        conn = self._make_conn([row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.hybrid_search("test")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["content"] == "test memory"
        assert "hybrid_score" in data[0]
        assert "keyword_score" in data[0]
        assert "semantic_score" in data[0]

    def test_custom_weights_accepted(self):
        row = {
            "id": 2, "content": "pure keyword match", "tags": [], "source": "y",
            "project": "", "created_at": datetime(2026, 1, 1),
            "keyword_score": 0.9, "semantic_score": 0.1, "hybrid_score": 0.9,
        }
        conn = self._make_conn([row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.hybrid_search("pure keyword match", keyword_weight=1.0, semantic_weight=0.0)
        data = json.loads(result)
        assert data[0]["id"] == 2


# ── Search cache tests ────────────────────────────────────────────────────────

class TestCacheHelpers:
    def setup_method(self):
        """Clear cache before each test."""
        server._cache_invalidate()

    def test_cache_miss_returns_none(self):
        assert server._cache_get(("semantic", "query", 10, 0.3, "", "", "")) is None

    def test_cache_set_and_get(self):
        key = ("semantic", "test query", 10, 0.3, "", "", "")
        server._cache_set(key, '["result"]')
        assert server._cache_get(key) == '["result"]'

    def test_cache_get_returns_none_after_invalidate(self):
        key = ("keyword", "hello", 10, "", "", "")
        server._cache_set(key, "result")
        server._cache_invalidate()
        assert server._cache_get(key) is None

    def test_cache_ttl_expiry(self):
        import time as _time
        key = ("semantic", "expiring", 10, 0.3, "", "", "")
        with patch("server.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            server._cache_set(key, "old result")
            # Advance time past TTL
            mock_time.monotonic.return_value = server.CACHE_TTL_SECONDS + 1
            result = server._cache_get(key)
        assert result is None

    def test_cache_lru_eviction_at_max_size(self):
        old_max = server.CACHE_MAX_SIZE
        try:
            server.CACHE_MAX_SIZE = 3
            for i in range(4):
                server._cache_set((f"key{i}",), f"val{i}")
            # key0 (oldest) should be evicted
            assert server._cache_get(("key0",)) is None
            assert server._cache_get(("key3",)) == "val3"
        finally:
            server.CACHE_MAX_SIZE = old_max

    def test_cache_move_to_end_on_hit(self):
        """Accessing an entry should protect it from LRU eviction."""
        old_max = server.CACHE_MAX_SIZE
        try:
            server.CACHE_MAX_SIZE = 3
            for i in range(3):
                server._cache_set((f"key{i}",), f"val{i}")
            # Access key0 to make it recently used
            server._cache_get(("key0",))
            # Add one more to trigger eviction — key1 should be evicted, not key0
            server._cache_set(("key3",), "val3")
            assert server._cache_get(("key0",)) == "val0"
            assert server._cache_get(("key1",)) is None
        finally:
            server.CACHE_MAX_SIZE = old_max


class TestSemanticSearchCache:
    def setup_method(self):
        server._cache_invalidate()

    def _make_search_conn(self, rows):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rows
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_cache_miss_calls_db(self):
        row = {"id": 1, "content": "test", "tags": [], "source": "x",
               "project": "", "created_at": datetime(2026, 1, 1), "similarity": 0.85}
        conn, cur = self._make_search_conn([row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            server.semantic_search("test query")
        cur.execute.assert_called_once()

    def test_cache_hit_skips_embed_and_db(self):
        row = {"id": 1, "content": "test", "tags": [], "source": "x",
               "project": "", "created_at": datetime(2026, 1, 1), "similarity": 0.85}
        conn, cur = self._make_search_conn([row])
        with patch("server.db_conn") as mock_db, patch("server.embed") as mock_embed:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            mock_embed.return_value = [0.0] * 768
            # First call — cache miss
            result1 = server.semantic_search("cached query")
            # Second call — should hit cache
            result2 = server.semantic_search("cached query")
        assert result1 == result2
        assert mock_embed.call_count == 1   # embedded only once
        assert cur.execute.call_count == 1  # DB queried only once

    def test_different_queries_cached_separately(self):
        conn, cur = self._make_search_conn([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            server.semantic_search("query A")
            server.semantic_search("query B")
        assert cur.execute.call_count == 2

    def test_cache_invalidated_after_save(self):
        # Prime the cache
        conn_search, cur_search = self._make_search_conn([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn_search)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            server.semantic_search("some query")
        assert len(server._search_cache) == 1

        # Now save a memory — cache should be cleared
        conn_write = MagicMock()
        cur_write = MagicMock()
        cur_write.__enter__ = MagicMock(return_value=cur_write)
        cur_write.__exit__ = MagicMock(return_value=False)
        cur_write.fetchone.side_effect = [None, {"id": 1, "created_at": datetime(2026, 1, 1), "deleted_at": None}]
        conn_write.cursor.return_value = cur_write
        conn_write.__enter__ = MagicMock(return_value=conn_write)
        conn_write.__exit__ = MagicMock(return_value=False)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn_write)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            server.save_memory("new memory")
        assert len(server._search_cache) == 0

    def test_cache_invalidated_after_delete(self):
        conn_search, _ = self._make_search_conn([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn_search)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            server.semantic_search("some query")
        assert len(server._search_cache) == 1

        conn_write = MagicMock()
        cur_write = MagicMock()
        cur_write.__enter__ = MagicMock(return_value=cur_write)
        cur_write.__exit__ = MagicMock(return_value=False)
        cur_write.rowcount = 1
        conn_write.cursor.return_value = cur_write
        conn_write.__enter__ = MagicMock(return_value=conn_write)
        conn_write.__exit__ = MagicMock(return_value=False)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn_write)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            server.delete_memory(1)
        assert len(server._search_cache) == 0


class TestFindDuplicates:
    def _make_conn(self, rows):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rows
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    def test_invalid_threshold_too_low(self):
        result = server.find_duplicates(threshold=0.3)
        assert "❌" in result

    def test_invalid_threshold_too_high(self):
        result = server.find_duplicates(threshold=1.1)
        assert "❌" in result

    def test_invalid_scan_limit(self):
        result = server.find_duplicates(scan_limit=5)
        assert "❌" in result

    def test_no_duplicates_returns_message(self):
        conn = self._make_conn([])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.find_duplicates()
        assert "No duplicate pairs" in result

    def test_returns_pairs_as_json(self):
        row = {
            "id_a": 1, "id_b": 2, "similarity": 0.92,
            "content_a": "hello world", "content_b": "hello world!",
            "created_a": datetime(2026, 1, 1), "created_b": datetime(2026, 1, 2),
        }
        conn = self._make_conn([row])
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.find_duplicates(threshold=0.85)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["id_a"] == 1
        assert data[0]["id_b"] == 2
        assert data[0]["similarity"] == 0.92


class TestBulkDelete:
    def _make_conn(self, total=0, preview_rows=None):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = preview_rows or []
        cur.fetchone.return_value = {"count": total}
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    def test_no_filters_returns_error(self):
        result = server.bulk_delete()
        assert "❌" in result

    def test_dry_run_does_not_commit(self):
        conn = self._make_conn(total=3)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.bulk_delete(tag="old", dry_run=True)
        conn.commit.assert_not_called()
        data = json.loads(result)
        assert data["total"] == 3
        assert "dry_run" in data["action"].lower()

    def test_dry_run_false_commits(self):
        conn = self._make_conn(total=2)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.bulk_delete(project="old-project", dry_run=False)
        conn.commit.assert_called_once()
        data = json.loads(result)
        assert data["total"] == 2
        assert "Deleted" in data["action"]

    def test_zero_matches_dry_run(self):
        conn = self._make_conn(total=0)
        with patch("server.db_conn") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=conn)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = server.bulk_delete(tag="nonexistent", dry_run=True)
        data = json.loads(result)
        assert data["total"] == 0
