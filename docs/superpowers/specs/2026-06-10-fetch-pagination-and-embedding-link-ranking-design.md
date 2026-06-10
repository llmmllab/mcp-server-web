# Spec: Fetch pagination + embedding-based link ranking

**Date:** 2026-06-10
**Status:** Approved (brainstorming) — ready for implementation plan
**Repo:** `mcp-server-web`
**Delivery:** Single push to `main` (auto-deploys the web MCP server pod). No PR.

## Problem

`fetch_page` and `fetch_with_links` both cap returned content at
`MAX_CONTENT_LENGTH` (20000 chars) and append `[Content truncated due to
length...]`. The marker is a dead end: it carries no total size, no offset,
and no way for the calling model to ask for the rest. A research agent that
wants more of a long page simply cannot get it.

Separately, `fetch_with_links` ranks outbound links by lexical anchor/URL-path
term overlap (`_link_score`). That is a crude bag-of-words signal; semantic
similarity would rank "what should I read next" far better.

## Goals

1. Let the model retrieve content **beyond** `MAX_CONTENT_LENGTH` from both
   tools via `offset`/`limit` pagination, with self-documenting "how to get the
   next chunk" metadata.
2. Add **optional** embedding-based ranking of `fetch_with_links` outbound
   links, with graceful fallback to the existing lexical ranking.

## Non-goals

- Paginating the **links** list (it stays a bounded ranked list — `max_links`,
  hard cap 60; its job is "what to read next", and top-N is enough).
- Changing `deep_research` (it never used `_truncate` and has its own passage
  caps — untouched).
- Per-page windows larger than `MAX_CONTENT_LENGTH` (the small-model context
  ceiling is preserved; `offset` is how you walk the whole document).

## Locked design decisions (from brainstorming)

| Fork | Decision |
|---|---|
| API surface | Optional `offset`/`limit` params on **both existing tools**. `offset=0`, default `limit` ⇒ today's behavior (backward compatible). |
| Page-2+ mechanism | **Re-fetch + small in-process TTL/LRU cache** of full extracted content. Cache miss transparently re-fetches. |
| Cut point | **Char-offset cursor**, window end **snapped** to `\n\n` → whitespace → hard-cut. |
| Link ranking wiring | **Hybrid**: config defaults (`EMBEDDING_*`) + optional per-call overrides. |
| Candidate-embedding cache | **Kept** — candidate vectors cached per model in the `links:{url}` cache value. |

## Architecture

Two new pure/standalone modules + one network module, consumed by the two
existing tools. Ranking and windowing stay pure/sync; all network I/O is
isolated in `fetch.py`/`_embeddings.py`.

```
config.py                      # + CONTENT_CACHE_*, EMBEDDING_* constants
tools/_content_cache.py  (new) # generic str->Any TTL+LRU cache (injectable clock)
tools/_pagination.py     (new) # pure window_text(full_text, offset, limit)
tools/_embeddings.py     (new) # async embed_texts(...) + pure cosine(...)
tools/fetch.py           (mod) # full-text extraction + cache + window; drop _truncate
tools/fetch_with_links.py(mod) # candidate split, cache value, pagination, embedding rank
server.py                (mod) # updated tool descriptions
```

---

## Component 1 — `tools/_content_cache.py`

A single-purpose, **value-agnostic** TTL + LRU cache (`str` key → arbitrary
value). Used by both tools with disjoint key namespaces and different value
shapes.

```python
class TTLCache:
    def __init__(self, *, ttl: float, max_entries: int, time_fn=time.monotonic): ...
    def get(self, key: str) -> Any | None        # None on miss/expired; lazy purge; LRU touch
    def put(self, key: str, value: Any) -> None   # LRU insert; evict oldest past max_entries
    def clear(self) -> None                       # test hygiene
```

- `OrderedDict`; `move_to_end` on `get`/`put`; pop oldest when over `max_entries`.
- TTL checked lazily in `get` against `time_fn()`; expired entry deleted, returns `None`.
- **Injectable `time_fn`** so unit tests fast-forward TTL deterministically.
- Module exposes a singleton `content_cache = TTLCache(ttl=CONTENT_CACHE_TTL,
  max_entries=CONTENT_CACHE_MAX_ENTRIES)`. Tools import the singleton.
- **No lock.** Two concurrent identical misses may both fetch + `put` (last
  write wins, identical content). Acceptable for a single-user research agent;
  documented, not solved.

Key namespaces:
- `fetch_page`        → `f"page:{int(render_js)}:{url}"`  → value: `str` (full extracted text)
- `fetch_with_links`  → `f"links:{url}"`                  → value: `dict` (see Component 4)

---

## Component 2 — `tools/_pagination.py`

Pure windowing function shared by both tools.

```python
def window_text(full_text: str, offset: int, limit: int) -> dict:
    # returns: {text, offset, returned_chars, total_chars, next_offset, has_more}
```

Algorithm:
1. `total = len(full_text)`; `offset = clamp(offset, 0, total)`;
   `limit = clamp(limit, 1, MAX_CONTENT_LENGTH)`.
2. If `offset >= total`: return empty window — `text=""`, `returned_chars=0`,
   `next_offset=None`, `has_more=False` (not an error).
3. `raw_end = min(offset + limit, total)`.
4. If `raw_end >= total`: `end = total` (last page, no snap).
   Else snap within `full_text[offset:raw_end]`, in order:
   - last `"\n\n"` at window-relative index `p > 0` → `end = offset + p + 2`
     (cut *after* the blank line so the next window starts on content);
   - else last whitespace (` `/`\n`/`\t`) at index `w > 0` → `end = offset + w + 1`;
   - else **hard cut** → `end = raw_end`.
5. **Progress guarantee:** every branch yields `end > offset` (snap indices are
   `> 0`; hard cut uses `raw_end = offset + limit` with `limit >= 1`).
6. `text = full_text[offset:end]`; `returned_chars = end - offset` (invariant:
   `len(text) == returned_chars`); `has_more = end < total`;
   `next_offset = end if has_more else None`.

No stripping of window text — keeps the `offset`/`returned_chars` arithmetic
exact. A trailing/leading blank line in a window is harmless.

---

## Component 3 — `tools/_embeddings.py`

Isolated network + math for semantic ranking. Network here; ranking stays pure.

```python
async def embed_texts(texts, *, endpoint, model, api_key, timeout) -> list[list[float]] | None
def cosine(a: list[float], b: list[float]) -> float
```

- `embed_texts`: one `POST {endpoint.rstrip('/')}/embeddings` with body
  `{"model": model, "input": texts}`; `Authorization: Bearer {api_key}` header
  only when `api_key` is truthy; `httpx` with `timeout`.
  Parse `resp.json()["data"]`, **sort by `index`**, return `[d["embedding"]]`.
  Return `None` on **any** failure (non-200, timeout, malformed, missing
  endpoint/model) — never raises to the caller.
- `cosine`: standard; zero-norm on either side → `0.0`.
- `EMBEDDING_ENDPOINT` is the OpenAI-compatible **base** (e.g. `…/v1`); we
  append `/embeddings`.

---

## Component 4 — `tools/fetch_with_links.py` changes

### Refactor: split link extraction into a cacheable, query-free collector + a pure ranker

```python
def _collect_link_candidates(html, page_url) -> list[dict]
    # query-INDEPENDENT: dedup, blocklist, anchor-length filters, same_domain.
    # each: {"url", "anchor", "same_domain"}

def _rank_links(candidates, query) -> list[dict]               # lexical (existing logic)
def _rank_links_semantic(candidates, query_vec, cand_vecs) -> list[dict]  # cosine + same-domain penalty

# Back-compat shim so existing unit tests stay green:
def _extract_outbound_links(html, page_url, query) -> list[dict]:
    return _rank_links(_collect_link_candidates(html, page_url), query)
```

- Lexical `_rank_links`: when `query` given, attach `relevance` (anchor/path
  overlap × 0.7 same-domain penalty), sort desc; when `query` is `None`, return
  in document order with **no** `relevance` field (unchanged behavior).
- `_rank_links_semantic`: `relevance = round(cosine(query_vec, v), 4)`, × 0.7 if
  `same_domain`, sort desc. Same output shape as lexical (so the link JSON shape
  is unchanged). Only runs when `query` is present.

### Cache value shape (`links:{url}`)

```python
{
  "content": str,                       # full extracted text (un-windowed)
  "link_candidates": list[dict],        # query-independent candidates
  "embeddings_by_model": {model: [vec, ...]},  # candidate vectors, aligned to link_candidates
}
```

`embeddings_by_model` starts `{}` and is filled per model on demand. Mutated in
place (stored by reference) so it persists without resetting TTL.

### Tool signature & flow

```python
async def fetch_with_links(
    url, query=None, max_links=20,
    offset=0, limit=None,
    rank_links_by="auto",            # "auto" | "embedding" | "lexical"
    embedding_endpoint=None, embedding_model=None, embedding_api_key=None,
) -> str
```

1. Validate URL (unchanged error JSON on bad scheme).
2. Cache `get("links:{url}")`. On miss: `fetch_html(url)` → if `None`, return the
   existing fetch-error JSON; else `content = _analyze_html(html)[0]`,
   `candidates = _collect_link_candidates(html, url)`, `put` the value.
3. **Resolve embedding settings (with key-leak guard):**
   - `endpoint = embedding_endpoint or EMBEDDING_ENDPOINT`
   - `model    = embedding_model    or EMBEDDING_MODEL`
   - `key`: if `embedding_endpoint` was passed (per-call override) →
     `embedding_api_key` only (**never** the config key); else →
     `embedding_api_key or EMBEDDING_API_KEY`.
   - `use_embedding = rank_links_by != "lexical" and bool(query) and bool(endpoint) and bool(model)`.
4. If `use_embedding`:
   - `cand_texts = [f"{c['anchor']} — {humanize_path(c['url'])}" for c in candidates]`
     where `humanize_path(u) = urlparse(u).path.replace('-',' ').replace('_',' ')`
     (anchor + the candidate's own URL path — the same signals `_link_score` uses).
   - candidate vectors: reuse `embeddings_by_model[model]` if present & length
     matches; else `embed_texts(cand_texts, ...)`. On `None` → fall back to
     lexical. On success → store under `embeddings_by_model[model]`.
   - query vector: `embed_texts([query], ...)` (separate call — see note). On
     `None` → fall back to lexical.
   - `ranked = _rank_links_semantic(candidates, query_vec, cand_vecs)`;
     `link_ranking = "embedding"`.
   - **Any** embedding failure ⇒ `ranked = _rank_links(candidates, query)`;
     `link_ranking = "lexical"`.
   else: `ranked = _rank_links(candidates, query)`; `link_ranking = "lexical"`.
5. `links = ranked[:clamp(max_links, 1, _HARD_MAX_LINKS)]`.
6. `win = window_text(content, offset, limit or MAX_CONTENT_LENGTH)`.
7. Return JSON envelope (see below).

> **Note (deliberate trade-off):** query and candidate embeddings are **two
> separate `embed_texts` calls** (not batched) so candidate vectors cache
> cleanly per model and the failure/cache paths are unit-testable. Cost: the
> first (uncached) call makes 2 embedding round-trips instead of 1. Subsequent
> paginated calls on the same URL make only the 1 query round-trip.

### Response JSON (additions in **bold**, existing fields preserved)

```jsonc
{
  "url": "...",
  "content": "...",                  // windowed slice
  "content_offset": 0,               // **new**
  "content_returned_chars": 20000,   // **new**
  "content_total_chars": 53210,      // **new**
  "content_has_more": true,          // **new**
  "content_next_offset": 20000,      // **new** (null when complete)
  "links": [ /* unchanged shape */ ],
  "link_ranking": "embedding"        // **new**: "embedding" | "lexical"
  // "error" path unchanged (content "", links [])
}
```

---

## Component 5 — `tools/fetch.py` changes

- **Remove `_truncate`.** Replace with `window_text`.
- Refactor so extraction returns the **full** text (no `"Content from…"` prefix,
  no truncation); windowing + formatting happen in `fetch_page`.
  - HTML path → full `_analyze_html` (+ existing SPA/Playwright fallback) text.
  - JSON / `text/*` path → full `body`.
  - Error cases (HTTP ≥400, bad content-type, timeout, network) → returned as
    today and **not cached**.

### Tool signature & flow

```python
async def fetch_page(url, render_js=False, offset=0, limit=None) -> str
```

1. Validate URL (unchanged).
2. Cache `get("page:{int(render_js)}:{url}")`. On miss: run the pipeline to get
   full text; on success `put` it; on error return the error string (no cache).
3. `win = window_text(full_text, offset, limit or MAX_CONTENT_LENGTH)`.
4. Format the response **string**:
   - **Common case** — `offset == 0 and not win["has_more"]` (whole doc fits one
     window): `f"Content from {url}:\n\n{text}"` — **identical to today**.
   - **Paginated case** (`end = offset + returned_chars`):
     ```
     Content from {url} [chars {offset}-{end} of {total}]:

     {window text}

     [More content available — call fetch_page again with offset={next_offset} to continue.]
     ```
     The footer line is omitted on the final page; the header range is still
     shown. `offset >= total` ⇒ header notes "offset N is at/after end of
     content (M chars)" with empty body.
   - The old `[Content truncated due to length...]` marker is **gone**.
5. Logging: include `offset`, `total`, `has_more` in the existing completion log.

---

## Component 6 — `config.py` additions

```python
CONTENT_CACHE_TTL         = float(os.environ.get("CONTENT_CACHE_TTL", "300"))
CONTENT_CACHE_MAX_ENTRIES = int(os.environ.get("CONTENT_CACHE_MAX_ENTRIES", "64"))

EMBEDDING_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "")   # OpenAI base, e.g. .../v1
EMBEDDING_MODEL    = os.environ.get("EMBEDDING_MODEL", "")
EMBEDDING_API_KEY  = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_TIMEOUT  = float(os.environ.get("EMBEDDING_TIMEOUT", "10"))
```

All `EMBEDDING_*` empty ⇒ embeddings off ⇒ lexical ranking (today's behavior).

---

## Component 7 — `server.py` / descriptions

Update `fetch_page` and `fetch_with_links` tool descriptions + docstrings so the
small models discover the new params:
- `fetch_page`: note that oversized content reports total size + the `offset` to
  call again with for the next chunk.
- `FETCH_WITH_LINKS_DESCRIPTION`: document `offset`/`limit` content pagination,
  the `content_*` response fields, and `rank_links_by` semantic ranking.

---

## Security note

The per-call `embedding_endpoint` override is an arbitrary URL the server will
`POST` to. The server already fetches arbitrary URLs, so this is not a new
*class* of risk — but the **config `EMBEDDING_API_KEY` is never sent to a
per-call-overridden endpoint** (only a per-call key is), preventing the
in-cluster key from leaking to a caller-supplied URL.

## Backward compatibility

- `fetch_page` with no new args: identical output for pages that fit one window;
  only the truncated-page footer text changes (now actionable).
- `fetch_with_links` with no new args: same `url`/`content`/`links`; adds
  `content_*` + `link_ranking` fields; `link_ranking="lexical"` by default
  (embeddings off unless configured).
- `_extract_outbound_links` kept as a shim → existing unit tests stay green.

## Testing plan (TDD)

New:
- `tests/test_content_cache.py` — put/get, TTL expiry via injected clock, LRU
  eviction at `max_entries`, `clear`.
- `tests/test_pagination.py` — basic slice; `\n\n` snap; whitespace fallback;
  hard-cut (one giant token / minified blob); `offset >= total` (empty, no
  error); offset/limit clamping; last-page `has_more=False`/`next_offset=None`;
  progress guarantee.
- `tests/test_embeddings.py` — `embed_texts` success (respx-mocked
  `/embeddings`), failure→`None` (500/timeout/malformed), index-ordering, auth
  header presence iff key; `cosine` math + zero-norm.

Update:
- `tests/test_fetch.py` — drop `TestTruncate` + `_truncate` import; add
  page-1-footer (with `offset=` hint), page-2-`offset`-returns-remainder,
  cache-hit-avoids-refetch (assert via respx call count), small-page output
  unchanged.
- `tests/test_fetch_with_links.py` — new `content_*` fields; links still
  returned on cache-hit page 2; `rank_links_by="lexical"` forces lexical even
  when configured; `"auto"` with endpoint+model+query uses embeddings
  (`embed_texts` mocked) → `link_ranking="embedding"` + cosine order; embedding
  failure → lexical fallback; **key-leak guard** (per-call endpoint override ⇒
  config key not passed); candidate-embedding cache reuse (candidate
  `embed_texts` invoked once across two calls on the same URL).

A `conftest` fixture clears `content_cache` between tests.

## CI note

`.github/workflows/ci.yml` compile-check lists files explicitly. Add the three
new modules:
`tools/_content_cache.py tools/_pagination.py tools/_embeddings.py`.

## Out of scope / future

- Link-list pagination (`link_offset`) — trivially addable if ever needed.
- Batching query+candidate embeddings into one round-trip (kept separate for
  cache/test clarity).
- Caching the query embedding (negligible; recomputed each call).
- Coalescing concurrent identical cache misses (single-user agent; not needed).
