"""Pure char-offset windowing for paginating extracted page content.

The cursor is a plain character offset into the full extracted text — simple
and monotonic.  When a window would cut mid-document, its end is snapped back
to a clean boundary (paragraph break, then whitespace, then a hard cut for
pathological blocks like minified JSON or one giant token) so the calling
model never receives a half-sentence and successive windows stitch together
exactly: ``full_text[a:b] + full_text[b:c] == full_text[a:c]``.

No I/O, no state — trivially testable.
"""

from __future__ import annotations

from typing import Optional

from config import MAX_CONTENT_LENGTH


def window_text(full_text: str, offset: int, limit: int) -> dict:
    """Return a window of ``full_text`` plus pagination metadata.

    Returns a dict: ``text``, ``offset`` (clamped start actually used),
    ``returned_chars`` (== ``len(text)``), ``total_chars``, ``next_offset``
    (the offset to call again with, or ``None`` when complete), ``has_more``.

    ``offset`` is clamped to ``[0, total]``; ``limit`` to
    ``[1, MAX_CONTENT_LENGTH]`` so a single call never exceeds the small-model
    context ceiling (``offset`` walks the whole document).
    """
    total = len(full_text)
    offset = max(0, min(offset, total))
    limit = max(1, min(limit, MAX_CONTENT_LENGTH))

    if offset >= total:
        return {
            "text": "",
            "offset": offset,
            "returned_chars": 0,
            "total_chars": total,
            "next_offset": None,
            "has_more": False,
        }

    raw_end = min(offset + limit, total)
    if raw_end >= total:
        end = total
    else:
        window = full_text[offset:raw_end]
        para = window.rfind("\n\n")
        if para > 0:
            end = offset + para + 2  # cut after the blank line
        else:
            ws = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
            if ws > 0:
                end = offset + ws + 1
            else:
                end = raw_end  # hard cut — guarantees progress (limit >= 1)

    text = full_text[offset:end]
    has_more = end < total
    next_offset: Optional[int] = end if has_more else None
    return {
        "text": text,
        "offset": offset,
        "returned_chars": end - offset,
        "total_chars": total,
        "next_offset": next_offset,
        "has_more": has_more,
    }
