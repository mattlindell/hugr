"""Tests for import_memories.py — pure-logic functions only (no DB or model required)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import import_memories as im


class TestExtractText:
    def test_plain_string_is_stripped(self):
        assert im.extract_text("  hello world  ") == "hello world"

    def test_empty_string(self):
        assert im.extract_text("") == ""

    def test_list_of_text_blocks(self):
        content = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        assert im.extract_text(content) == "first\nsecond"

    def test_list_skips_non_text_blocks(self):
        content = [
            {"type": "tool_result", "text": "ignored"},
            {"type": "text", "text": "kept"},
        ]
        assert im.extract_text(content) == "kept"

    def test_empty_list(self):
        assert im.extract_text([]) == ""

    def test_list_with_no_text_type(self):
        content = [{"type": "image", "url": "http://example.com/img.png"}]
        assert im.extract_text(content) == ""

    def test_non_string_non_list_returns_empty(self):
        assert im.extract_text(None) == ""
        assert im.extract_text(42) == ""
        assert im.extract_text({"type": "text"}) == ""
