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
