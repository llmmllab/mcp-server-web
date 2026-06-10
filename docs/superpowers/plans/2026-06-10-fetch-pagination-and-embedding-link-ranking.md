# Fetch Pagination + Embedding Link Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `fetch_page` and `fetch_with_links` page through content beyond `MAX_CONTENT_LENGTH` via `offset`/`limit`, and add optional embedding-based semantic ranking of `fetch_with_links` outbound links.

**Architecture:** Three new focused modules — a value-agnostic TTL+LRU cache, a pure char-offset windowing function, and an OpenAI-compatible embeddings client — are consumed by the two existing tools. Pagination re-fetches on cache miss but serves page-2+ from the cache; ranking and windowing stay pure/sync while all network I/O is isolated in `fetch.py`/`_embeddings.py`.

**Tech Stack:** Python 3.12, FastMCP, httpx, BeautifulSoup, pytest + respx, `uv`.

**Spec:** `docs/superpowers/specs/2026-06-10-fetch-pagination-and-embedding-link-ranking-design.md`

**Delivery:** All tasks committed locally; a single `git push origin main` at the end triggers CI + the auto-deploy that rolls the `mcp-server-web` pod. Do NOT push until Task 11 passes.

**Test/lint commands:**
- Single test: `uv run pytest tests/test_X.py::TestClass::test_name -v`
- File: `uv run pytest tests/test_X.py -v`
- Full suite: `uv run pytest tests/ -v`
- Lint: `uv run ruff check .` / `uv run ruff format --check .`

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `config.py` | Modify | + `CONTENT_CACHE_*`, `EMBEDDING_*` constants |
| `tools/_content_cache.py` | Create | Generic `str -> Any` TTL+LRU cache (injectable clock) + process singleton |
| `tools/_pagination.py` | Create | Pure `window_text(full_text, offset, limit)` with boundary snapping |
| `tools/_embeddings.py` | Create | `async embed_texts(...)` (OpenAI-compatible) + pure `cosine(...)` |
| `tools/fetch.py` | Modify | Full-text extraction + cache + window; drop `_truncate` |
| `tools/fetch_with_links.py` | Modify | Candidate split, cache value, content pagination, embedding ranking |
| `server.py` | Modify | Updated tool descriptions |
| `tests/conftest.py` | Modify | Autouse fixture clearing the content cache between tests |
| `tests/test_content_cache.py` | Create | Cache unit tests |
| `tests/test_pagination.py` | Create | Windowing unit tests |
| `tests/test_embeddings.py` | Create | Embeddings client + cosine unit tests |
| `tests/test_config.py` | Modify | New config defaults/overrides |
| `tests/test_fetch.py` | Modify | Replace truncation tests with pagination tests |
| `tests/test_fetch_with_links.py` | Modify | Content pagination + embedding ranking tests |
| `.github/workflows/ci.yml` | Modify | Add new modules to compile-check |
| `README.md` | Modify | Document new params/fields/env vars; fix stale default |

---

## Task 1: Config constants

**Files:**
- Modify: `config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add these two methods inside the existing `TestConfigDefaults` class in `tests/test_config.py`:

```python
    def test_content_cache_and_embedding_defaults(self, monkeypatch):
        for key in list(os.environ.keys()):
            if key.startswith(("CONTENT_CACHE_", "EMBEDDING_")):
                monkeypatch.delenv(key, raising=False)
        import config  # noqa: F401
        reload(config)

        assert config.CONTENT_CACHE_TTL == 300.0
        assert config.CONTENT_CACHE_MAX_ENTRIES == 64
        assert config.EMBEDDING_ENDPOINT == ""
        assert config.EMBEDDING_MODEL == ""
        assert config.EMBEDDING_API_KEY == ""
        assert config.EMBEDDING_TIMEOUT == 10.0

    def test_content_cache_and_embedding_env_overrides(self, monkeypatch):
        monkeypatch.setenv("CONTENT_CACHE_TTL", "60")
        monkeypatch.setenv("CONTENT_CACHE_MAX_ENTRIES", "8")
        monkeypatch.setenv("EMBEDDING_ENDPOINT", "http://emb/v1")
        monkeypatch.setenv("EMBEDDING_MODEL", "bge")
        monkeypatch.setenv("EMBEDDING_TIMEOUT", "3")
        import config  # noqa: F401
        reload(config)

        assert config.CONTENT_CACHE_TTL == 60.0
        assert config.CONTENT_CACHE_MAX_ENTRIES == 8
        assert config.EMBEDDING_ENDPOINT == "http://emb/v1"
        assert config.EMBEDDING_MODEL == "bge"
        assert config.EMBEDDING_TIMEOUT == 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'CONTENT_CACHE_TTL'`

- [ ] **Step 3: Add the constants**

Append to `config.py` after the `FETCH_HARD_TIMEOUT` block (after line 44):

```python

# Content cache — lets the fetch tools paginate a large page across calls
# without re-fetching (and re-rendering) it every time.  See
# tools/_content_cache.py.
CONTENT_CACHE_TTL = float(os.environ.get("CONTENT_CACHE_TTL", "300"))
CONTENT_CACHE_MAX_ENTRIES = int(os.environ.get("CONTENT_CACHE_MAX_ENTRIES", "64"))

# Optional embedding endpoint for semantic ranking of fetch_with_links
# outbound links.  All-empty => embeddings off => lexical ranking (default).
# EMBEDDING_ENDPOINT is the OpenAI-compatible base (e.g. ".../v1"); the
# embeddings module appends "/embeddings".
EMBEDDING_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_TIMEOUT = float(os.environ.get("EMBEDDING_TIMEOUT", "10"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all config tests green)

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat(config): add CONTENT_CACHE_* and EMBEDDING_* settings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: TTL+LRU content cache

**Files:**
- Create: `tools/_content_cache.py`
- Test: `tests/test_content_cache.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_content_cache.py`:

```python
"""Tests for tools/_content_cache.py — the TTL + LRU cache."""

from tools._content_cache import TTLCache


class _Clock:
    """Manually-advanced fake monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_put_then_get_returns_value():
    c = TTLCache(ttl=100, max_entries=10)
    c.put("k", "v")
    assert c.get("k") == "v"


def test_missing_key_returns_none():
    c = TTLCache(ttl=100, max_entries=10)
    assert c.get("nope") is None


def test_entry_expires_after_ttl():
    clock = _Clock()
    c = TTLCache(ttl=100, max_entries=10, time_fn=clock)
    c.put("k", "v")
    clock.advance(99)
    assert c.get("k") == "v"      # still live
    clock.advance(1)              # now at +100 == expires_at
    assert c.get("k") is None     # expired


def test_lru_eviction_past_max_entries():
    c = TTLCache(ttl=1000, max_entries=2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)                 # evicts least-recently-used "a"
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_get_refreshes_lru_recency():
    c = TTLCache(ttl=1000, max_entries=2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1        # "a" is now most-recently-used
    c.put("c", 3)                 # evicts "b", not "a"
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3


def test_clear_drops_everything():
    c = TTLCache(ttl=1000, max_entries=10)
    c.put("a", 1)
    c.clear()
    assert c.get("a") is None


def test_stores_arbitrary_value_types():
    c = TTLCache(ttl=1000, max_entries=10)
    payload = {"content": "x", "links": [1, 2, 3]}
    c.put("k", payload)
    assert c.get("k") is payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_content_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools._content_cache'`

- [ ] **Step 3: Create the module**

Create `tools/_content_cache.py`:

```python
"""Tiny in-process TTL + LRU cache shared by the fetch tools.

The MCP server is stateless across HTTP calls, but paginating a large page
without re-fetching (and re-rendering) it on every ``offset`` call needs a
place to keep the full extracted content between calls.  This is that place:
a value-agnostic ``str`` key -> arbitrary value cache with a time-to-live and
a bounded LRU eviction policy, so memory stays capped and stale entries fall
out on their own.

Deliberately tiny and dependency-free.  No lock: two concurrent identical
misses may both compute and ``put`` the same value (last write wins, identical
content) — acceptable for a single-user research agent.  The clock is
injectable so tests can fast-forward TTL deterministically.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from config import CONTENT_CACHE_MAX_ENTRIES, CONTENT_CACHE_TTL


class TTLCache:
    """A bounded ``str -> Any`` cache with per-entry TTL and LRU eviction."""

    def __init__(
        self,
        *,
        ttl: float,
        max_entries: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max(1, max_entries)
        self._time_fn = time_fn
        # key -> (expires_at, value); OrderedDict preserves LRU order.
        self._store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        """Return the live value for ``key`` or ``None`` (miss or expired).

        Expired entries are purged on access; a live hit is moved to the
        most-recently-used end.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._time_fn() >= expires_at:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value

    def put(self, key: str, value: Any) -> None:
        """Insert/refresh ``key``; evict the oldest entry if over capacity."""
        expires_at = self._time_fn() + self._ttl
        self._store[key] = (expires_at, value)
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        """Drop all entries (test hygiene)."""
        self._store.clear()


# Process-wide singleton the fetch tools share.
content_cache = TTLCache(
    ttl=CONTENT_CACHE_TTL,
    max_entries=CONTENT_CACHE_MAX_ENTRIES,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_content_cache.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/_content_cache.py tests/test_content_cache.py
git commit -m "feat(cache): add TTL+LRU content cache for fetch pagination

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Pagination windowing

**Files:**
- Create: `tools/_pagination.py`
- Test: `tests/test_pagination.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pagination.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pagination.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools._pagination'`

- [ ] **Step 3: Create the module**

Create `tools/_pagination.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pagination.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/_pagination.py tests/test_pagination.py
git commit -m "feat(pagination): add boundary-snapping window_text helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Embeddings client + cosine

**Files:**
- Create: `tools/_embeddings.py`
- Test: `tests/test_embeddings.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embeddings.py`:

```python
"""Tests for tools/_embeddings.py — OpenAI-compatible client + cosine."""

import httpx
import pytest
import respx

from tools._embeddings import cosine, embed_texts


def test_cosine_identical():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_cosine_orthogonal():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_zero_norm():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_mismatched_length():
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_success():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={"data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]},
        )
    )
    vecs = await embed_texts(
        ["a", "b"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_orders_by_index():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={"data": [
                {"index": 1, "embedding": [9.0]},
                {"index": 0, "embedding": [1.0]},
            ]},
        )
    )
    vecs = await embed_texts(
        ["a", "b"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs == [[1.0], [9.0]]


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_non_200_returns_none():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(500)
    )
    vecs = await embed_texts(
        ["a"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs is None


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_count_mismatch_returns_none():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0]}]}
        )
    )
    vecs = await embed_texts(
        ["a", "b"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs is None


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_sends_auth_header_when_key():
    captured = {}

    def _capture(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0]}]}
        )

    respx.post("http://emb/v1/embeddings").mock(side_effect=_capture)
    await embed_texts(
        ["a"], endpoint="http://emb/v1", model="m", api_key="secret", timeout=5
    )
    assert captured["auth"] == "Bearer secret"


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_no_auth_header_without_key():
    captured = {}

    def _capture(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0]}]}
        )

    respx.post("http://emb/v1/embeddings").mock(side_effect=_capture)
    await embed_texts(
        ["a"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert captured["auth"] is None


@pytest.mark.asyncio
async def test_embed_texts_guards_empty_inputs():
    assert await embed_texts([], endpoint="http://emb/v1", model="m", api_key="", timeout=5) is None
    assert await embed_texts(["a"], endpoint="", model="m", api_key="", timeout=5) is None
    assert await embed_texts(["a"], endpoint="http://emb/v1", model="", api_key="", timeout=5) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_embeddings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools._embeddings'`

- [ ] **Step 3: Create the module**

Create `tools/_embeddings.py`:

```python
"""OpenAI-compatible embeddings client + cosine similarity.

Isolates the only network call in the semantic link-ranking path so the
ranking itself can stay pure/sync and unit-testable.  ``embed_texts`` returns
``None`` on *any* failure (missing config, non-200, timeout, malformed body)
so callers can fall back to lexical ranking without try/except gymnastics.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional, Sequence

import httpx

logger = logging.getLogger("mcp-server-web.embeddings")


async def embed_texts(
    texts: Sequence[str],
    *,
    endpoint: str,
    model: str,
    api_key: str,
    timeout: float,
) -> Optional[list[list[float]]]:
    """Embed ``texts`` via a single OpenAI-compatible ``POST .../embeddings``.

    ``endpoint`` is the base URL (e.g. ``.../v1``); ``/embeddings`` is
    appended.  Returns one vector per input (ordered by the response
    ``index`` field), or ``None`` on any failure.
    """
    if not texts or not endpoint or not model:
        return None

    url = endpoint.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "input": list(texts)}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            logger.debug("embed_texts non-200 (%s) from %s", resp.status_code, url)
            return None
        data = resp.json().get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            return None
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in ordered]
    except (httpx.RequestError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
        logger.debug("embed_texts failed for %s: %s", url, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("embed_texts unexpected error for %s: %s", url, e)
        return None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 on mismatched length or a zero vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_embeddings.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/_embeddings.py tests/test_embeddings.py
git commit -m "feat(embeddings): add OpenAI-compatible embed_texts + cosine

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `fetch_page` pagination

**Files:**
- Modify: `tools/fetch.py` (remove `_truncate`, `_process_html`, `_fetch_impl`; add `_fetch_full`, `_format_page`; rewrite `fetch_page`)
- Modify: `tests/conftest.py` (autouse cache-clear fixture)
- Modify: `tests/test_fetch.py` (drop `_truncate` tests, add pagination tests)

- [ ] **Step 1: Add the cache-clear fixture to conftest**

Append to `tests/conftest.py`:

```python

import pytest


@pytest.fixture(autouse=True)
def _clear_content_cache():
    """Each test starts with an empty fetch cache so a page cached by one test
    can't bleed into another (and so respx call-count assertions hold)."""
    from tools._content_cache import content_cache

    content_cache.clear()
    yield
    content_cache.clear()
```

- [ ] **Step 2: Rewrite the failing tests in `tests/test_fetch.py`**

Change the import line at the top from:

```python
from tools.fetch import fetch_page, _analyze_html, _truncate
```

to:

```python
from tools.fetch import fetch_page, _analyze_html
```

Delete the entire `class TestTruncate:` block (the three `test_no_truncation_when_short` / `test_truncates_when_too_long` / `test_exact_length_not_truncated` methods).

Delete the existing `test_truncation_applied` method inside `TestFetchPage`.

Add these methods inside `class TestFetchPage:`:

```python
    @respx.mock
    @pytest.mark.asyncio
    async def test_small_page_output_unchanged(self):
        html = "<html><body><h1>Title</h1><p>Short content</p></body></html>"
        respx.get("https://example.com/small").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )
        result = await fetch_page("https://example.com/small")
        assert result.startswith("Content from https://example.com/small:\n\n")
        assert "[chars" not in result
        assert "More content available" not in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_pagination_first_page_has_offset_footer(self):
        long_html = "<html><body><p>" + "x" * (MAX_CONTENT_LENGTH + 5000) + "</p></body></html>"
        respx.get("https://example.com/long").mock(
            return_value=httpx.Response(200, text=long_html, headers={"content-type": "text/html"})
        )
        result = await fetch_page("https://example.com/long")
        assert "[chars 0-" in result
        assert "call fetch_page again with offset=" in result
        assert "[Content truncated due to length...]" not in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_pagination_offset_returns_remainder(self):
        head = "A" * MAX_CONTENT_LENGTH
        tail = "B" * 3000
        long_html = f"<html><body><p>{head}{tail}</p></body></html>"
        respx.get("https://example.com/long2").mock(
            return_value=httpx.Response(200, text=long_html, headers={"content-type": "text/html"})
        )
        page1 = await fetch_page("https://example.com/long2")
        m = re.search(r"offset=(\d+)", page1)
        assert m is not None
        next_off = int(m.group(1))
        page2 = await fetch_page("https://example.com/long2", offset=next_off)
        assert "B" in page2
        assert "call fetch_page again with offset=" not in page2  # last page

    @respx.mock
    @pytest.mark.asyncio
    async def test_pagination_uses_cache_no_refetch(self):
        head = "A" * MAX_CONTENT_LENGTH
        tail = "B" * 3000
        long_html = f"<html><body><p>{head}{tail}</p></body></html>"
        route = respx.get("https://example.com/cached").mock(
            return_value=httpx.Response(200, text=long_html, headers={"content-type": "text/html"})
        )
        await fetch_page("https://example.com/cached")
        await fetch_page("https://example.com/cached", offset=MAX_CONTENT_LENGTH)
        assert route.call_count == 1
```

Add `import re` to the top of `tests/test_fetch.py` (below the existing imports).

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: FAIL — `ImportError: cannot import name '_truncate'` is resolved by the edit, but the new tests fail (e.g. `test_pagination_first_page_has_offset_footer` finds the old `[Content truncated…]` marker / no `[chars 0-`).

- [ ] **Step 4: Rewrite `tools/fetch.py`**

Replace the `_truncate`, `_process_html`, `_fetch_impl`, and `fetch_page` definitions (lines 206-209, 239-249, 295-323, 326-382) with the following. Keep everything else (imports, boilerplate helpers, `_analyze_html`, `_render_with_playwright`, `fetch_html`) unchanged. Add these imports near the top with the other `from config`/`from tools` imports:

```python
from config import MAX_CONTENT_LENGTH  # already imported — keep
from tools._content_cache import content_cache
from tools._pagination import window_text
```

Replace `_truncate` (delete it) and `_process_html`/`_fetch_impl` with:

```python
async def _fetch_full(url: str, render_js: bool) -> tuple[Optional[str], Optional[str]]:
    """Fetch ``url`` and return its FULL extracted text (no truncation, no
    "Content from" prefix), or an error message.  Exactly one of
    ``(full_text, error)`` is non-None.

    HTML runs through ``_analyze_html`` (+ Playwright SPA fallback on the
    non-render path); JSON / text bodies are returned verbatim.  This is the
    cacheable unit that ``fetch_page`` windows for pagination.
    """
    if render_js:
        rendered = await _render_with_playwright(url)
        if not rendered:
            return None, "Error: Playwright rendering failed or not installed."
        text, _ = _analyze_html(rendered)
        return text, None

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        follow_redirects=True,
    ) as client:
        response = await client.get(url, headers=BROWSER_HEADERS)
        if response.status_code >= 400:
            return None, f"Error: HTTP {response.status_code} when accessing {url}"

        content_type = (response.headers.get("content-type") or "").lower()
        body = response.text

        if "text/html" in content_type:
            text, is_spa = _analyze_html(body)
            if is_spa:
                rendered = await _render_with_playwright(url)
                if rendered:
                    text, _ = _analyze_html(rendered)
            return text, None

        if "application/json" in content_type or "text/" in content_type:
            return body, None

        return None, (
            f"Error: URL does not appear to contain readable text "
            f"(content-type: {content_type})"
        )


def _format_page(url: str, win: dict) -> str:
    """Render a window dict (from ``window_text``) into the tool's response.

    The common case — the whole document fits one window at offset 0 — is
    byte-identical to the pre-pagination output.  Paginated responses gain a
    ``[chars X-Y of N]`` header and, when more remains, an actionable footer.
    """
    offset = win["offset"]
    total = win["total_chars"]
    text = win["text"]

    if offset == 0 and not win["has_more"]:
        return f"Content from {url}:\n\n{text}"

    if win["returned_chars"] == 0:
        return (
            f"Content from {url} "
            f"[offset {offset} is at/after end of content ({total} chars)]:\n\n"
        )

    end = offset + win["returned_chars"]
    body = f"Content from {url} [chars {offset}-{end} of {total}]:\n\n{text}"
    if win["has_more"]:
        body += (
            f"\n\n[More content available — call fetch_page again with "
            f"offset={win['next_offset']} to continue.]"
        )
    return body
```

Replace `fetch_page` with:

```python
async def fetch_page(
    url: str,
    render_js: bool = False,
    offset: int = 0,
    limit: Optional[int] = None,
) -> str:
    """
    Read and extract text content from a web page URL, with pagination.

    Handles HTML (static and SPA), plain text/markdown, JSON, and other
    text-based content. Auto-detects JavaScript-rendered pages and can fall
    back to Playwright rendering.

    Long pages are paginated. Each call returns at most ``limit`` characters
    (default/maximum ``MAX_CONTENT_LENGTH``) starting at ``offset``. When more
    content remains, the response reports the total size and the exact
    ``offset`` to call again with. The full extracted page is cached briefly
    so paging through it does not re-fetch (or re-render) the URL.

    Args:
        url: The URL to read content from (must be http:// or https://).
        render_js: If True, skip httpx and render with Playwright directly.
        offset: Character offset into the extracted text to start at (default 0).
        limit: Max characters to return this call (default/cap MAX_CONTENT_LENGTH).

    Returns:
        Clean text content from the web page (with a pagination header/footer
        when the content spans multiple windows), or an error message.
    """
    try:
        parsed_url = urlparse(url)
        if not parsed_url.scheme or parsed_url.scheme not in ("http", "https"):
            return f"Error: Invalid URL '{url}'. Only HTTP and HTTPS URLs are supported."
    except Exception as e:
        return f"Error: Invalid URL format '{url}': {str(e)}"

    cache_key = f"page:{int(render_js)}:{url}"
    full_text = content_cache.get(cache_key)

    if full_text is None:
        try:
            full_text, error = await asyncio.wait_for(
                _fetch_full(url, render_js), timeout=FETCH_HARD_TIMEOUT
            )
        except asyncio.TimeoutError:
            msg = (
                f"Error: Timeout when trying to access {url} "
                f"({FETCH_HARD_TIMEOUT} seconds, hard cap)"
            )
            logger.warning(msg, extra={"url": url})
            return msg
        except httpx.RequestError as e:
            msg = f"Error: Network error when accessing {url}: {str(e)}"
            logger.warning(msg, extra={"url": url})
            return msg
        except Exception as e:
            msg = f"Error: Failed to read content from {url}: {str(e)}"
            logger.error(msg, extra={"url": url})
            return msg
        if error is not None:
            return error  # errors are returned, never cached
        content_cache.put(cache_key, full_text)

    win = window_text(full_text, offset, limit if limit is not None else MAX_CONTENT_LENGTH)
    result = _format_page(url, win)
    logger.info(
        "fetch_page completed",
        extra={
            "url": url,
            "render_js": render_js,
            "offset": win["offset"],
            "total_chars": win["total_chars"],
            "has_more": win["has_more"],
            "result_bytes": len(result),
        },
    )
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: PASS (all existing fetch tests + 4 new pagination tests)

- [ ] **Step 6: Commit**

```bash
git add tools/fetch.py tests/test_fetch.py tests/conftest.py
git commit -m "feat(fetch_page): paginate via offset/limit backed by content cache

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Split link extraction (refactor, no behavior change)

**Files:**
- Modify: `tools/fetch_with_links.py` (split `_extract_outbound_links` into `_collect_link_candidates` + `_rank_links`, keep a back-compat shim)
- Test: `tests/test_fetch_with_links.py` (existing `TestExtractOutboundLinks` must stay green; add direct tests for the split helpers)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fetch_with_links.py`, importing the new helpers — change:

```python
from tools.fetch_with_links import (
    _extract_outbound_links,
    fetch_with_links,
)
```

to:

```python
from tools.fetch_with_links import (
    _collect_link_candidates,
    _extract_outbound_links,
    _rank_links,
    fetch_with_links,
)
```

Add a new test class:

```python
class TestCollectAndRank:
    def test_collect_is_query_independent(self):
        html = """
        <html><body>
          <a href="https://other.com/x">External link here</a>
          <a href="https://source.com/y">Internal link here</a>
        </body></html>
        """
        cands = _collect_link_candidates(html, "https://source.com/post")
        by_url = {c["url"]: c for c in cands}
        assert by_url["https://source.com/y"]["same_domain"] is True
        assert by_url["https://other.com/x"]["same_domain"] is False
        # No relevance attached at collection time.
        assert all("relevance" not in c for c in cands)

    def test_rank_lexical_attaches_relevance_and_sorts(self):
        cands = [
            {"url": "https://e.com/banana", "anchor": "Banana bread", "same_domain": False},
            {"url": "https://e.com/python-async", "anchor": "Python async guide", "same_domain": False},
        ]
        ranked = _rank_links(cands, query="python async")
        assert ranked[0]["url"] == "https://e.com/python-async"
        assert ranked[0]["relevance"] > 0
        scores = [r["relevance"] for r in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_rank_no_query_keeps_order_and_no_relevance(self):
        cands = [
            {"url": "https://e.com/a", "anchor": "A link", "same_domain": False},
            {"url": "https://e.com/b", "anchor": "B link", "same_domain": False},
        ]
        ranked = _rank_links(cands, query=None)
        assert [r["url"] for r in ranked] == ["https://e.com/a", "https://e.com/b"]
        assert all("relevance" not in r for r in ranked)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch_with_links.py -v`
Expected: FAIL — `ImportError: cannot import name '_collect_link_candidates'`

- [ ] **Step 3: Refactor `_extract_outbound_links`**

In `tools/fetch_with_links.py`, replace the entire `_extract_outbound_links` function (lines 137-210) with the three definitions below. The collection logic and filters are copied verbatim from the original; only the query-dependent scoring is split out into `_rank_links`.

```python
def _collect_link_candidates(html: str, page_url: str) -> list[dict]:
    """Pull query-INDEPENDENT outbound link candidates from the page body.

    Filters (unchanged from the original extractor):
      - skip ``#fragment``, ``mailto:``, ``tel:``, ``javascript:`` URLs
      - skip blocklisted URLs (socials, login, file extensions)
      - require anchor text in ``[_MIN_ANCHOR_CHARS, _MAX_ANCHOR_CHARS]``
      - dedupe by URL (keep the first occurrence's anchor), strip fragments

    Returns candidates in document order, each ``{"url", "anchor",
    "same_domain"}``.  Scoring/ranking is applied separately so the candidate
    list can be cached and re-ranked per call (lexical or embedding).
    """
    soup = BeautifulSoup(html, "html.parser")
    page_domain = urlparse(page_url).netloc.lower().removeprefix("www.")

    seen_urls: set[str] = set()
    candidates: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            absolute = urljoin(page_url, href)
        except Exception:
            continue
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if _is_blocklisted_url(absolute):
            continue
        absolute = absolute.split("#", 1)[0]
        if absolute in seen_urls:
            continue
        anchor = a.get_text(" ", strip=True)
        if not (_MIN_ANCHOR_CHARS <= len(anchor) <= _MAX_ANCHOR_CHARS):
            continue
        seen_urls.add(absolute)
        target_domain = urlparse(absolute).netloc.lower().removeprefix("www.")
        candidates.append(
            {
                "url": absolute,
                "anchor": anchor,
                "same_domain": target_domain == page_domain,
            }
        )
    return candidates


def _rank_links(candidates: list[dict], query: Optional[str]) -> list[dict]:
    """Lexical ranking (anchor + URL-path term overlap).

    With a ``query``: attach ``relevance`` (× 0.7 same-domain penalty) and sort
    descending.  Without one (or with an all-stopword query): return candidates
    in document order with no ``relevance`` field — unchanged legacy behavior.
    """
    if not query:
        return [dict(c) for c in candidates]
    query_terms = _query_terms(query)
    if not query_terms:
        return [dict(c) for c in candidates]
    ranked: list[dict] = []
    for c in candidates:
        score = _link_score(c["anchor"], c["url"], query_terms)
        if c["same_domain"]:
            score *= 0.7
        ranked.append({**c, "relevance": round(score, 4)})
    ranked.sort(key=lambda e: e.get("relevance", 0.0), reverse=True)
    return ranked


def _extract_outbound_links(
    html: str,
    page_url: str,
    query: Optional[str],
) -> list[dict]:
    """Back-compat wrapper: collect candidates then lexically rank them."""
    return _rank_links(_collect_link_candidates(html, page_url), query)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fetch_with_links.py -v`
Expected: PASS — the original `TestExtractOutboundLinks` cases AND the new `TestCollectAndRank` cases all green.

- [ ] **Step 5: Commit**

```bash
git add tools/fetch_with_links.py tests/test_fetch_with_links.py
git commit -m "refactor(fetch_with_links): split link collection from ranking

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `fetch_with_links` content pagination + caching

**Files:**
- Modify: `tools/fetch_with_links.py` (add `offset`/`limit`, cache value dict, `content_*` + `link_ranking` fields)
- Test: `tests/test_fetch_with_links.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestFetchWithLinks` in `tests/test_fetch_with_links.py`:

```python
    async def test_content_pagination_fields_and_offset(self):
        head = "A" * 20000  # == default MAX_CONTENT_LENGTH window
        tail = "B" * 2000
        html = (
            f"<html><body><article><p>{head}{tail}</p>"
            f"<a href='https://x.com/y'>link text here</a></article></body></html>"
        )
        with patch(
            "tools.fetch_with_links.fetch_html",
            new=AsyncMock(return_value=html),
        ) as m:
            p1 = json.loads(await fetch_with_links("https://example.com/a", query="alpha"))
            p2 = json.loads(
                await fetch_with_links(
                    "https://example.com/a", query="alpha", offset=p1["content_next_offset"]
                )
            )
        assert p1["content_offset"] == 0
        assert p1["content_total_chars"] == 22000
        assert p1["content_has_more"] is True
        assert p1["content_next_offset"] == 20000
        assert p1["link_ranking"] == "lexical"
        assert "B" in p2["content"]
        assert p2["content_has_more"] is False
        assert p2["content_next_offset"] is None
        # Links served from cache on page 2 — identical, and only one fetch.
        assert p2["links"] == p1["links"]
        assert m.await_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch_with_links.py::TestFetchWithLinks::test_content_pagination_fields_and_offset -v`
Expected: FAIL — `KeyError: 'content_next_offset'` (field not yet emitted)

- [ ] **Step 3: Add imports and rewrite `fetch_with_links`**

In `tools/fetch_with_links.py`, update the imports block (lines 45-49) to add the cache + pagination helpers:

```python
from config import FETCH_HARD_TIMEOUT, MAX_CONTENT_LENGTH
from tools._content_cache import content_cache
from tools._pagination import window_text
from tools.fetch import (
    _analyze_html,
    fetch_html,
)
```

Replace the entire `fetch_with_links` coroutine (lines 224-314) with:

```python
async def fetch_with_links(
    url: str,
    query: Optional[str] = None,
    max_links: int = _DEFAULT_MAX_LINKS,
    offset: int = 0,
    limit: Optional[int] = None,
) -> str:
    """Fetch ``url`` and return windowed text content + outbound links as JSON.

    Args:
        url: The page to fetch (http:// or https:// only).
        query: Optional research question. When set, outbound links are ranked
            by relevance. The text content is NOT filtered by ``query``.
        max_links: Maximum links returned (capped at ``_HARD_MAX_LINKS``).
        offset: Character offset into the extracted content (default 0).
        limit: Max content characters this call (default/cap MAX_CONTENT_LENGTH).

    Returns:
        JSON envelope ``{"url", "content", "content_offset",
        "content_returned_chars", "content_total_chars", "content_has_more",
        "content_next_offset", "links": [...], "link_ranking", "error"?}``.
        The full extracted page + link candidates are cached briefly so paging
        through content does not re-fetch the URL.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return json.dumps(
                {
                    "url": url,
                    "error": f"Invalid URL '{url}': only http/https supported.",
                    "content": "",
                    "links": [],
                }
            )
    except Exception as e:
        return json.dumps(
            {"url": url, "error": f"Invalid URL: {e}", "content": "", "links": []}
        )

    max_links = max(1, min(max_links, _HARD_MAX_LINKS))

    cache_key = f"links:{url}"
    cached = content_cache.get(cache_key)
    if cached is None:
        try:
            html = await asyncio.wait_for(fetch_html(url), timeout=FETCH_HARD_TIMEOUT)
        except asyncio.TimeoutError:
            return json.dumps(
                {
                    "url": url,
                    "error": f"Fetch timed out after {FETCH_HARD_TIMEOUT}s",
                    "content": "",
                    "links": [],
                }
            )
        if not html:
            return json.dumps(
                {
                    "url": url,
                    "error": "Failed to fetch page (HTTP error, non-HTML content, or network error).",
                    "content": "",
                    "links": [],
                }
            )
        cached = {
            "content": _analyze_html(html)[0],
            "link_candidates": _collect_link_candidates(html, url),
            "embeddings_by_model": {},
        }
        content_cache.put(cache_key, cached)

    content_full = cached["content"]
    candidates = cached["link_candidates"]

    ranked = _rank_links(candidates, query)
    link_ranking = "lexical"
    links = ranked[:max_links]

    win = window_text(content_full, offset, limit if limit is not None else MAX_CONTENT_LENGTH)

    logger.info(
        "fetch_with_links completed",
        extra={
            "url": url,
            "query": query,
            "content_offset": win["offset"],
            "content_total_chars": win["total_chars"],
            "content_has_more": win["has_more"],
            "links_returned": len(links),
            "link_ranking": link_ranking,
        },
    )
    return json.dumps(
        {
            "url": url,
            "content": win["text"],
            "content_offset": win["offset"],
            "content_returned_chars": win["returned_chars"],
            "content_total_chars": win["total_chars"],
            "content_has_more": win["has_more"],
            "content_next_offset": win["next_offset"],
            "links": links,
            "link_ranking": link_ranking,
        }
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fetch_with_links.py -v`
Expected: PASS (existing `TestFetchWithLinks` cases + the new pagination case)

- [ ] **Step 5: Commit**

```bash
git add tools/fetch_with_links.py tests/test_fetch_with_links.py
git commit -m "feat(fetch_with_links): paginate content + cache page for re-ranking

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Embedding-based link ranking

**Files:**
- Modify: `tools/fetch_with_links.py` (add `rank_links_by` + `embedding_*` params, `_humanize_path`, `_rank_links_semantic`, `_rank_links_with_embeddings`, resolution logic with key-leak guard)
- Test: `tests/test_fetch_with_links.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestFetchWithLinks` in `tests/test_fetch_with_links.py`. The shared HTML fixture has two links — one matching the query "alpha", one not:

```python
    _EMBED_HTML = (
        "<html><body><article><p>Body text long enough to extract.</p>"
        "<a href='https://e.com/alpha-topic'>Alpha topic deep dive</a>"
        "<a href='https://e.com/beta-thing'>Beta thing overview</a>"
        "</article></body></html>"
    )

    async def _fake_embed(self, texts, **kwargs):
        # "alpha" -> [1,0]; everything else -> [0,1].
        return [[1.0, 0.0] if "alpha" in t.lower() else [0.0, 1.0] for t in texts]

    async def test_auto_uses_embeddings_when_configured(self, monkeypatch):
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_ENDPOINT", "http://emb/v1")
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_MODEL", "m")
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_API_KEY", "")
        with patch("tools.fetch_with_links.fetch_html", new=AsyncMock(return_value=self._EMBED_HTML)), \
             patch("tools.fetch_with_links.embed_texts", new=AsyncMock(side_effect=self._fake_embed)):
            res = json.loads(await fetch_with_links("https://src.com/p", query="alpha"))
        assert res["link_ranking"] == "embedding"
        assert res["links"][0]["url"] == "https://e.com/alpha-topic"
        assert res["links"][0]["relevance"] > res["links"][1]["relevance"]

    async def test_rank_links_by_lexical_forces_lexical(self, monkeypatch):
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_ENDPOINT", "http://emb/v1")
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_MODEL", "m")
        embed = AsyncMock(side_effect=self._fake_embed)
        with patch("tools.fetch_with_links.fetch_html", new=AsyncMock(return_value=self._EMBED_HTML)), \
             patch("tools.fetch_with_links.embed_texts", new=embed):
            res = json.loads(
                await fetch_with_links("https://src.com/p", query="alpha", rank_links_by="lexical")
            )
        assert res["link_ranking"] == "lexical"
        embed.assert_not_called()

    async def test_embedding_failure_falls_back_to_lexical(self, monkeypatch):
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_ENDPOINT", "http://emb/v1")
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_MODEL", "m")
        with patch("tools.fetch_with_links.fetch_html", new=AsyncMock(return_value=self._EMBED_HTML)), \
             patch("tools.fetch_with_links.embed_texts", new=AsyncMock(return_value=None)):
            res = json.loads(await fetch_with_links("https://src.com/p", query="alpha"))
        assert res["link_ranking"] == "lexical"

    async def test_per_call_endpoint_does_not_leak_config_key(self, monkeypatch):
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_ENDPOINT", "")
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_MODEL", "")
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_API_KEY", "CONFIG-SECRET")
        embed = AsyncMock(side_effect=self._fake_embed)
        with patch("tools.fetch_with_links.fetch_html", new=AsyncMock(return_value=self._EMBED_HTML)), \
             patch("tools.fetch_with_links.embed_texts", new=embed):
            await fetch_with_links(
                "https://src.com/p", query="alpha",
                embedding_endpoint="http://other/v1", embedding_model="m",
            )
        # Config key must never be sent to a caller-supplied endpoint.
        for call in embed.await_args_list:
            assert call.kwargs["api_key"] != "CONFIG-SECRET"
            assert call.kwargs["api_key"] == ""

    async def test_candidate_embeddings_cached_across_calls(self, monkeypatch):
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_ENDPOINT", "http://emb/v1")
        monkeypatch.setattr("tools.fetch_with_links.EMBEDDING_MODEL", "m")
        embed = AsyncMock(side_effect=self._fake_embed)
        with patch("tools.fetch_with_links.fetch_html", new=AsyncMock(return_value=self._EMBED_HTML)), \
             patch("tools.fetch_with_links.embed_texts", new=embed):
            await fetch_with_links("https://src.com/p", query="alpha")  # candidates(1) + query(1)
            await fetch_with_links("https://src.com/p", query="alpha")  # query(1), candidates cached
        # 2 + 1 == 3 calls; a 4th would mean candidates were re-embedded.
        assert embed.await_count == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch_with_links.py -v`
Expected: FAIL — `TypeError: fetch_with_links() got an unexpected keyword argument 'rank_links_by'`

- [ ] **Step 3: Add helpers and the embedding branch**

In `tools/fetch_with_links.py`, extend the imports added in Task 7 to include the embedding settings + client:

```python
from config import (
    EMBEDDING_API_KEY,
    EMBEDDING_ENDPOINT,
    EMBEDDING_MODEL,
    EMBEDDING_TIMEOUT,
    FETCH_HARD_TIMEOUT,
    MAX_CONTENT_LENGTH,
)
from tools._content_cache import content_cache
from tools._embeddings import cosine, embed_texts
from tools._pagination import window_text
from tools.fetch import (
    _analyze_html,
    fetch_html,
)
```

Add these three helpers immediately above `_rank_links` (they belong with the ranking logic):

```python
def _humanize_path(u: str) -> str:
    """URL path with separators turned into spaces — extra signal for embedding."""
    return urlparse(u).path.replace("-", " ").replace("_", " ")


def _rank_links_semantic(
    candidates: list[dict],
    query_vec: list[float],
    cand_vecs: list[list[float]],
) -> list[dict]:
    """Rank candidates by cosine(query, candidate) with the same-domain penalty.

    Same output shape as ``_rank_links`` (a ``relevance`` float per link) so the
    response JSON is identical regardless of which ranker ran.
    """
    ranked: list[dict] = []
    for c, v in zip(candidates, cand_vecs):
        score = cosine(query_vec, v)
        if c["same_domain"]:
            score *= 0.7
        ranked.append({**c, "relevance": round(score, 4)})
    ranked.sort(key=lambda e: e.get("relevance", 0.0), reverse=True)
    return ranked


async def _rank_links_with_embeddings(
    candidates: list[dict],
    query: str,
    *,
    endpoint: str,
    model: str,
    api_key: str,
    cache_value: dict,
) -> Optional[list[dict]]:
    """Semantically rank ``candidates``; return ``None`` to signal lexical fallback.

    Candidate vectors are cached per model in ``cache_value['embeddings_by_model']``
    (mutated in place) so paginating the same URL does not re-embed the links.
    The query vector is embedded fresh each call (1 input, negligible).
    """
    cand_texts = [f"{c['anchor']} — {_humanize_path(c['url'])}" for c in candidates]

    cached_vecs = cache_value["embeddings_by_model"].get(model)
    if cached_vecs is not None and len(cached_vecs) == len(candidates):
        cand_vecs = cached_vecs
    else:
        cand_vecs = await embed_texts(
            cand_texts, endpoint=endpoint, model=model, api_key=api_key,
            timeout=EMBEDDING_TIMEOUT,
        )
        if cand_vecs is None:
            return None
        cache_value["embeddings_by_model"][model] = cand_vecs

    query_vecs = await embed_texts(
        [query], endpoint=endpoint, model=model, api_key=api_key,
        timeout=EMBEDDING_TIMEOUT,
    )
    if not query_vecs:
        return None
    return _rank_links_semantic(candidates, query_vecs[0], cand_vecs)
```

Update the `fetch_with_links` signature to add the new params:

```python
async def fetch_with_links(
    url: str,
    query: Optional[str] = None,
    max_links: int = _DEFAULT_MAX_LINKS,
    offset: int = 0,
    limit: Optional[int] = None,
    rank_links_by: str = "auto",
    embedding_endpoint: Optional[str] = None,
    embedding_model: Optional[str] = None,
    embedding_api_key: Optional[str] = None,
) -> str:
```

Replace the ranking block from Task 7 — these three lines:

```python
    ranked = _rank_links(candidates, query)
    link_ranking = "lexical"
    links = ranked[:max_links]
```

with the resolution + embedding branch:

```python
    # Resolve embedding settings (hybrid: per-call overrides config). Key-leak
    # guard: a per-call endpoint override never receives the config API key.
    endpoint = embedding_endpoint or EMBEDDING_ENDPOINT
    model = embedding_model or EMBEDDING_MODEL
    if embedding_endpoint:
        api_key = embedding_api_key or ""
    else:
        api_key = embedding_api_key or EMBEDDING_API_KEY

    use_embedding = (
        rank_links_by != "lexical"
        and bool(query)
        and bool(endpoint)
        and bool(model)
        and bool(candidates)
    )

    ranked = None
    link_ranking = "lexical"
    if use_embedding:
        semantic = await _rank_links_with_embeddings(
            candidates, query, endpoint=endpoint, model=model,
            api_key=api_key, cache_value=cached,
        )
        if semantic is not None:
            ranked, link_ranking = semantic, "embedding"
    if ranked is None:
        ranked = _rank_links(candidates, query)
    links = ranked[:max_links]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fetch_with_links.py -v`
Expected: PASS (all five new embedding cases + everything prior)

- [ ] **Step 5: Commit**

```bash
git add tools/fetch_with_links.py tests/test_fetch_with_links.py
git commit -m "feat(fetch_with_links): optional embedding-based link ranking

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Tool descriptions & docstrings

**Files:**
- Modify: `server.py` (the `fetch_page` registration description)
- Modify: `tools/fetch_with_links.py` (`FETCH_WITH_LINKS_DESCRIPTION`)

- [ ] **Step 1: Update the `fetch_page` description in `server.py`**

Replace the `mcp.tool(name="fetch_page", ...)` description (lines 43-49) with:

```python
mcp.tool(
    name="fetch_page",
    description=(
        "Fetch and extract readable text content from a web page URL. "
        "Handles static HTML, SPAs (via Playwright), plain text, and JSON. "
        "Long pages are paginated: the response reports the character range "
        "and total size, and when more remains it tells you the exact 'offset' "
        "to call again with. Pass 'offset' (and optional 'limit') to read "
        "beyond the per-call cap."
    ),
)(fetch_page)
```

- [ ] **Step 2: Update `FETCH_WITH_LINKS_DESCRIPTION`**

Replace the `FETCH_WITH_LINKS_DESCRIPTION` constant (lines 214-221) in `tools/fetch_with_links.py` with:

```python
FETCH_WITH_LINKS_DESCRIPTION = (
    "Fetch a web page and return BOTH its cleaned text AND its outbound "
    "links (with anchor text and optional relevance score against a "
    "query).  The intended pattern is turn-by-turn LLM-driven research: "
    "fetch a page, decide which link to follow next based on what you "
    "just read, fetch that, repeat.  Pass 'query' to score links by "
    "relevance; omit it for raw link-graph output.  Content is paginated — "
    "the response carries 'content_total_chars'/'content_has_more'/"
    "'content_next_offset'; pass 'offset' (and optional 'limit') to read "
    "beyond the per-call cap.  Leave 'rank_links_by' at \"auto\" (or set "
    "\"embedding\") to rank links semantically when an embedding endpoint is "
    "configured; it falls back to lexical ranking otherwise."
)
```

- [ ] **Step 3: Verify nothing broke (descriptions are strings — run the suite)**

Run: `uv run pytest tests/ -v`
Expected: PASS (full suite)

- [ ] **Step 4: Commit**

```bash
git add server.py tools/fetch_with_links.py
git commit -m "docs(tools): document pagination + embedding ranking in descriptions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: CI compile-check + README

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`

- [ ] **Step 1: Add new modules to the CI compile-check**

In `.github/workflows/ci.yml`, replace the `Compile-check` run block:

```yaml
      - name: Compile-check
        run: |
          uv run python -m py_compile \
            server.py config.py \
            tools/search.py tools/fetch.py \
            tools/fetch_with_links.py tools/deep_research.py
```

with:

```yaml
      - name: Compile-check
        run: |
          uv run python -m py_compile \
            server.py config.py \
            tools/search.py tools/fetch.py \
            tools/fetch_with_links.py tools/deep_research.py \
            tools/_content_cache.py tools/_pagination.py tools/_embeddings.py
```

- [ ] **Step 2: Update `README.md` — `fetch_page` params**

In `README.md`, replace the `fetch_page` arg bullets (lines 19-20):

```markdown
- **url** (required): URL to fetch (http/https only)
- **render_js**: Force Playwright rendering (default: false)
```

with:

```markdown
- **url** (required): URL to fetch (http/https only)
- **render_js**: Force Playwright rendering (default: false)
- **offset**: Character offset to start at, for paging through long pages (default: 0)
- **limit**: Max characters returned this call (default/cap: `MAX_CONTENT_LENGTH`)

Long pages are paginated: the response carries a `[chars X-Y of N]` header and, when more remains, an actionable footer telling the model the exact `offset` to call again with. The full extracted page is cached briefly (`CONTENT_CACHE_TTL`) so paging does not re-fetch or re-render the URL.
```

- [ ] **Step 3: Update `README.md` — `fetch_with_links` params + return shape**

Replace the `fetch_with_links` arg bullets and Returns line (lines 27-31):

```markdown
- **url** (required): The page to fetch (http/https only)
- **query**: Optional research question; when set, outbound links are scored and sorted by anchor + URL-path overlap with the query terms (content is not filtered — the model is in a better position to decide what's relevant once it has read the page)
- **max_links**: Maximum outbound links returned (default 20, clamped to [1, 60])

Returns JSON: `{"url", "content", "links": [{"url", "anchor", "same_domain", "relevance"?}, ...]}`.
```

with:

```markdown
- **url** (required): The page to fetch (http/https only)
- **query**: Optional research question; when set, outbound links are ranked by relevance (content is not filtered — the model decides what's relevant once it has read the page)
- **max_links**: Maximum outbound links returned (default 20, clamped to [1, 60])
- **offset** / **limit**: Content pagination, same semantics as `fetch_page` (default offset 0, cap `MAX_CONTENT_LENGTH`)
- **rank_links_by**: `auto` (default) | `embedding` | `lexical`. `auto`/`embedding` rank links by semantic similarity when an embedding endpoint resolves and a `query` is present, else lexical
- **embedding_endpoint** / **embedding_model** / **embedding_api_key**: Optional per-call overrides of the `EMBEDDING_*` config. A per-call endpoint override never receives the config API key

Returns JSON: `{"url", "content", "content_offset", "content_returned_chars", "content_total_chars", "content_has_more", "content_next_offset", "links": [{"url", "anchor", "same_domain", "relevance"?}, ...], "link_ranking"}` where `link_ranking` is `"embedding"` or `"lexical"`.
```

- [ ] **Step 4: Update `README.md` — config table**

Replace the `MAX_CONTENT_LENGTH` row (line 74):

```markdown
| `MAX_CONTENT_LENGTH` | `8000` | Max chars returned from fetch |
```

with these rows (corrects the stale default and adds the new settings):

```markdown
| `MAX_CONTENT_LENGTH` | `20000` | Max chars returned per fetch call (pagination window cap) |
| `CONTENT_CACHE_TTL` | `300` | Seconds a fetched page stays cached for pagination |
| `CONTENT_CACHE_MAX_ENTRIES` | `64` | Max pages held in the content cache (LRU) |
| `EMBEDDING_ENDPOINT` | _(empty)_ | OpenAI-compatible base URL (e.g. `.../v1`) for semantic link ranking; empty ⇒ lexical |
| `EMBEDDING_MODEL` | _(empty)_ | Embedding model name |
| `EMBEDDING_API_KEY` | _(empty)_ | Bearer token for the embedding endpoint |
| `EMBEDDING_TIMEOUT` | `10` | Embedding request timeout (seconds) |
```

- [ ] **Step 5: Verify the workflow YAML is valid and the suite still passes**

Run: `uv run pytest tests/ -v`
Expected: PASS (full suite — README/CI changes don't affect tests)

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml README.md
git commit -m "docs+ci: document pagination/embeddings; compile-check new modules

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Full verification + push (single deploy)

**Files:** none (verification + deploy)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS — every test green (config, content_cache, pagination, embeddings, fetch, fetch_with_links, search, server, deep_research).

- [ ] **Step 2: Compile-check exactly as CI does**

Run:
```bash
uv run python -m py_compile \
  server.py config.py \
  tools/search.py tools/fetch.py \
  tools/fetch_with_links.py tools/deep_research.py \
  tools/_content_cache.py tools/_pagination.py tools/_embeddings.py
```
Expected: no output, exit 0.

- [ ] **Step 3: Lint + format check**

Run: `uv run ruff check .` then `uv run ruff format --check .`
Expected: clean (or only pre-existing warnings). Fix any new issues this work introduced, then re-run the suite and amend the relevant commit.

- [ ] **Step 4: Confirm the working tree is clean and review the log**

Run: `git status` (expect clean) and `git log --oneline origin/main..HEAD` (expect the spec commit + Tasks 1-10).

- [ ] **Step 5: Push to main (triggers CI + auto-deploy)**

Run: `git push origin main`
Then watch the deploy: `gh run watch` (or `gh run list --limit 3`). The deploy rolls `deployment/mcp-server-web` in the `llmmllab` namespace.

- [ ] **Step 6: Post-deploy smoke check (optional but recommended)**

Once the rollout completes, exercise pagination against a known-long page via the running researcher agent or a direct MCP call, confirming a `content_has_more: true` response yields the next chunk when re-called with `content_next_offset`.

---

## Self-Review

**Spec coverage:**
- Component 1 (TTL+LRU cache) → Task 2. ✓
- Component 2 (`window_text`) → Task 3. ✓
- Component 3 (`embed_texts`/`cosine`) → Task 4. ✓
- Component 4 (`fetch_with_links` split + pagination + embedding rank + key-leak guard + candidate-embedding cache) → Tasks 6, 7, 8. ✓
- Component 5 (`fetch_page` full-text extraction + cache + window + formatting; drop `_truncate`) → Task 5. ✓
- Component 6 (config constants) → Task 1. ✓
- Component 7 (descriptions/docstrings) → Task 9. ✓
- Security note (key-leak guard) → Task 8 (`test_per_call_endpoint_does_not_leak_config_key`). ✓
- Testing plan (all new/updated test files) → Tasks 1-8. ✓
- CI compile-check note → Task 10. ✓
- Backward compat (small-page output unchanged, shim keeps old tests green) → Task 5 (`test_small_page_output_unchanged`), Task 6 (`_extract_outbound_links` shim). ✓

**Type/name consistency:** `window_text` returns `{text, offset, returned_chars, total_chars, next_offset, has_more}` (Task 3) — consumed verbatim by `_format_page` (Task 5) and mapped to `content_*` fields (Task 7). `_collect_link_candidates`/`_rank_links`/`_rank_links_semantic`/`_rank_links_with_embeddings` names are consistent across Tasks 6-8. `content_cache` singleton and `embed_texts`/`cosine` names match their defining tasks. Cache value dict keys `content`/`link_candidates`/`embeddings_by_model` consistent Tasks 7-8.

**Placeholder scan:** none — every code/test step contains complete content.
