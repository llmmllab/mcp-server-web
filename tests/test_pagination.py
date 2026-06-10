"""Tests for tools/_pagination.py — char-offset windowing with snapping."""

from tools._pagination import window_text
from config import MAX_CONTENT_LENGTH


def test_short_text_single_page():
    w = window_text("hello world", 0, 100)
    assert w["text"] == "hello world"
    assert w["offset"] == 0
    assert w["total_chars"] == 11
    assert w["returned_chars"] == 11
    assert w["has_more"] is False
    assert w["next_offset"] is None


def test_snaps_to_paragraph_boundary():
    full = "para one here\n\npara two here\n\npara three"
    w = window_text(full, 0, 20)
    # window[0:20] = "para one here\n\npara "; last \n\n at idx 13 -> end = 15
    assert w["text"] == "para one here\n\n"
    assert w["next_offset"] == 15
    assert w["has_more"] is True


def test_whitespace_fallback_when_no_paragraph():
    full = "word " * 10  # 50 chars, single spaces, no blank line
    w = window_text(full, 0, 12)
    # window[0:12] = "word word wo"; last space at idx 9 -> end = 10
    assert w["text"] == "word word "
    assert w["next_offset"] == 10
    assert w["has_more"] is True


def test_hard_cut_when_no_boundary():
    full = "x" * 100
    w = window_text(full, 0, 30)
    assert w["text"] == "x" * 30
    assert w["next_offset"] == 30
    assert w["has_more"] is True


def test_offset_beyond_end_returns_empty():
    w = window_text("short", 100, 50)
    assert w["text"] == ""
    assert w["returned_chars"] == 0
    assert w["has_more"] is False
    assert w["next_offset"] is None
    assert w["offset"] == 5  # clamped to total


def test_last_page_has_no_more():
    full = "x" * 100
    w = window_text(full, 80, 50)
    assert w["text"] == "x" * 20
    assert w["has_more"] is False
    assert w["next_offset"] is None


def test_limit_clamped_to_max_content_length():
    full = "x" * (MAX_CONTENT_LENGTH + 5000)
    w = window_text(full, 0, MAX_CONTENT_LENGTH + 99999)
    assert w["returned_chars"] == MAX_CONTENT_LENGTH  # clamped down
    assert w["has_more"] is True


def test_negative_offset_clamped_to_zero():
    w = window_text("hello", -5, 100)
    assert w["offset"] == 0
    assert w["text"] == "hello"


def test_walk_entire_doc_reassembles():
    full = "a" * 10 + "b" * 10 + "c" * 10  # 30 chars, no whitespace
    chunks = []
    off = 0
    while off is not None:
        w = window_text(full, off, 10)
        if w["returned_chars"] == 0:
            break
        chunks.append(w["text"])
        off = w["next_offset"]
    assert "".join(chunks) == full
