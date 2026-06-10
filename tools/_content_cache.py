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
