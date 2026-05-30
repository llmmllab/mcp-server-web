"""
Deep research tool — iterative web search with content extraction and link following.

The MCP server has no LLM client of its own, so "intelligence" here is heuristic
and structural rather than reasoning-based.  The calling LLM does the actual
synthesis; this tool's job is to do the *mechanical* breadth-first expansion
that an LLM would otherwise have to drive turn-by-turn, returning a structured
bundle of scored passages and the link graph that produced them.

Strategy — synthesised from public deep-research designs:

  • OpenAI Deep Research (Plan-Act-Observe loop)
  • GPT Researcher (planner → executor → publisher, 20+ source aggregation)
  • qx-labs IterativeResearcher (Knowledge Gap → Tool Selector → Observations)

Algorithm
---------
1. **Round 0 (search)**: issue ``query`` to SearxNG, take top-``num_seeds``
   results.  Their URLs form the round-0 frontier.
2. **For each round 1..max_depth**:
   a. Fetch every URL in the current frontier concurrently (bounded by a
      semaphore so we don't DDoS).
   b. Per page, run :func:`extract_passages_and_links`: split into
      paragraphs, score each by query-term density, keep top-K
      passages; extract outbound ``<a href>`` links and score by
      anchor-text + URL-path overlap with the query.
   c. Promote the top scored outbound links to the next round's
      frontier — filtered for: same-URL dedup, same-domain
      pagination patterns, social/share/login URLs, max two links
      per source page, no more than ``max_per_domain`` links per
      domain across the whole run (source diversity).
3. **Termination**: stops at whichever of these hits first —
   ``max_depth``, ``max_pages``, ``max_seconds`` wall-clock budget,
   or empty frontier.
4. **Synthesis**: dedupe near-identical passages (shingle-based),
   sort by relevance × depth-weight, return as a structured JSON
   envelope the calling LLM can render directly into a report.

The output is intentionally LLM-readable JSON — no markdown formatting,
no narrative summary — because we don't have an LLM here to write one
and the caller is in a better position to do it with full conversation
context.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from tools.fetch import (
    _BLOCK_TAGS,
    _BOILERPLATE_CLASS_RE,
    _extract_blocks,
    _strip_boilerplate,
    fetch_html,
)
from tools.search import SearxError, searx_query

logger = logging.getLogger("mcp-server-web.deep_research")


# ---------------------------------------------------------------------------
# Tuning knobs (env-overridable wouldn't be useful — these are per-call)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_DEPTH = 2
_DEFAULT_MAX_BREADTH = 6
_DEFAULT_MAX_PAGES = 30
_DEFAULT_MAX_SECONDS = 180
_DEFAULT_NUM_SEEDS = 8

# Concurrency cap for the page-fetch fan-out.  Above ~5 some hosts
# rate-limit; below 3 the wall-clock blows out.  4 is a good middle.
_FETCH_CONCURRENCY = 4

# Per-page extraction caps.
_MAX_PASSAGES_PER_PAGE = 5
_MAX_LINKS_PER_PAGE = 4
# Source diversity: no one domain dominates the frontier.
_MAX_LINKS_PER_DOMAIN = 4

# Passage shape: short snippets confuse synthesis (no context), very
# long ones bloat the response.  This window covers typical paragraphs.
# 80 chars catches real prose while still filtering one-line nav text
# and image captions; 1200 is roughly the length of a long paragraph.
_PASSAGE_MIN_CHARS = 80
_PASSAGE_MAX_CHARS = 1200

# Shingle size for near-dup detection across pages.  5-gram on words
# catches paraphrases of the same source paragraph reasonably well.
_SHINGLE_WORDS = 5

# URL patterns that are almost never useful content (avoid burning the
# fetch budget on them).
_URL_BLOCKLIST_PATTERNS = re.compile(
    r"""(?xi)
    (?:^|[/.])(?:
        facebook|twitter|x|instagram|linkedin|youtube|tiktok|pinterest|reddit
        |whatsapp|telegram|messenger|threads
    )(?:\.|/)
    |
    /(?:login|signin|signup|register|logout|cart|checkout|account|profile)(?:/|$|\?)
    |
    /(?:share|email|print|favorite|like|subscribe)(?:/|$|\?)
    |
    \.(?:pdf|zip|tar|gz|exe|dmg|mp4|mp3|jpg|jpeg|png|gif|svg|ico)(?:\?|$)
    """
)

# Generic stopword list — small enough to keep inline, large enough to
# kill the common false-positive scoring matches.
_STOPWORDS = frozenset(
    """
    the a an and or but if then else of in on at to for from by with as is are
    was were be been being have has had do does did will would should could may
    might must can this that these those i you he she it we they me him her us
    them my your his their our its which who whom whose what when where why how
    not no nor so too very about into over under between out up down off out
    all any some most more such only own same than then once just also
    """.split()
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LinkCandidate:
    """An outbound link discovered on a fetched page."""

    url: str
    anchor: str
    score: float
    parent_url: str
    depth: int


@dataclass
class _Passage:
    """A scored text snippet extracted from a page."""

    text: str
    source_url: str
    score: float
    depth: int

    def shingle(self) -> frozenset[str]:
        """N-gram fingerprint for near-dup detection."""
        words = [
            w for w in re.findall(r"\w+", self.text.lower())
            if w not in _STOPWORDS and len(w) > 1
        ]
        if len(words) < _SHINGLE_WORDS:
            return frozenset()
        return frozenset(
            " ".join(words[i:i + _SHINGLE_WORDS])
            for i in range(len(words) - _SHINGLE_WORDS + 1)
        )


@dataclass
class _ResearchState:
    """Mutable state threaded through the BFS loop."""

    query: str
    query_terms: frozenset[str]
    visited: set[str] = field(default_factory=set)
    passages: list[_Passage] = field(default_factory=list)
    page_count: int = 0
    domain_counts: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    fetch_errors: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _normalize_terms(text: str) -> list[str]:
    """Tokenise → lowercase → drop stopwords + short tokens."""
    return [
        w for w in re.findall(r"\w+", text.lower())
        if w not in _STOPWORDS and len(w) > 1
    ]


def _query_terms(query: str) -> frozenset[str]:
    return frozenset(_normalize_terms(query))


def _passage_score(passage: str, query_terms: frozenset[str]) -> float:
    """Score a paragraph by query-term density.

    Density beats raw count — a 100-char snippet with 3 hits is more
    relevant than a 5000-char wall-of-text with 4 hits.  We also reward
    longer passages slightly because they tend to carry more context.

    Returns 0 if no overlap; otherwise a positive float roughly in
    [0.01, 5].
    """
    if not query_terms:
        return 0.0
    tokens = _normalize_terms(passage)
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in query_terms)
    if hits == 0:
        return 0.0
    density = hits / len(tokens)
    # Length bonus: log-shaped, saturating around 800 chars.
    length_factor = min(1.0, len(passage) / 800.0)
    coverage = len({t for t in tokens if t in query_terms}) / max(1, len(query_terms))
    return density * (0.5 + length_factor) * (0.5 + coverage)


def _link_score(
    anchor: str,
    url: str,
    query_terms: frozenset[str],
) -> float:
    """Score an outbound link by anchor-text + URL-path overlap."""
    if not query_terms:
        return 0.0
    parsed = urlparse(url)
    path_terms = _normalize_terms(parsed.path.replace("-", " ").replace("_", " "))
    anchor_terms = _normalize_terms(anchor)
    if not (path_terms or anchor_terms):
        return 0.0
    anchor_hits = sum(1 for t in anchor_terms if t in query_terms)
    path_hits = sum(1 for t in path_terms if t in query_terms)
    # Anchor matches are stronger evidence of relevance than path tokens.
    return anchor_hits * 1.0 + path_hits * 0.4


def _domain_of(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc.removeprefix("www.")


def _is_blocklisted_url(url: str) -> bool:
    return bool(_URL_BLOCKLIST_PATTERNS.search(url))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


# Boilerplate-strip helpers (``_BLOCK_TAGS``, ``_BOILERPLATE_CLASS_RE``,
# ``_strip_boilerplate``) are imported from :mod:`tools.fetch` above so
# the single-page fetch tool and the deep-research crawl apply the
# same filtering — no drift between them.


def _normalize_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_passages_and_links(
    html: str,
    page_url: str,
    query_terms: frozenset[str],
    depth: int,
) -> tuple[list[_Passage], list[_LinkCandidate]]:
    """Extract scored passages + scored outbound links from an HTML page.

    Steps:
      1. Prune boilerplate (semantic tags + class/id patterns).
      2. Split remaining body into block-tag-aligned paragraphs.
      3. Drop paragraphs outside ``[_PASSAGE_MIN_CHARS, _PASSAGE_MAX_CHARS]``.
      4. Score by query-term density, keep the top
         ``_MAX_PASSAGES_PER_PAGE``.
      5. Collect ``<a href>`` outbound links, score by anchor + path
         overlap, take the top ``_MAX_LINKS_PER_PAGE`` after blocklist
         filtering.
    """
    soup = BeautifulSoup(html, "html.parser")
    _strip_boilerplate(soup)

    # --- passages -------------------------------------------------
    # Use the same leaf-block extraction + adjacent-dup dedup as
    # ``fetch_page`` (tools/fetch.py::_extract_blocks).  Then apply
    # the deep-research-specific length filter so we only keep
    # passages that have enough context to be useful in synthesis.
    paragraphs: list[str] = []
    for block in _extract_blocks(soup):
        if _PASSAGE_MIN_CHARS <= len(block) <= _PASSAGE_MAX_CHARS:
            paragraphs.append(block)

    scored = [
        (_passage_score(p, query_terms), p) for p in paragraphs
    ]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(reverse=True)
    passages = [
        _Passage(text=p, source_url=page_url, score=s, depth=depth)
        for s, p in scored[:_MAX_PASSAGES_PER_PAGE]
    ]

    # --- outbound links -------------------------------------------
    link_candidates: list[_LinkCandidate] = []
    page_domain = _domain_of(page_url)
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
        anchor = a.get_text(" ", strip=True)
        # Skip links with no anchor text (often icon-only nav).
        if not anchor or len(anchor) > 200:
            continue
        score = _link_score(anchor, absolute, query_terms)
        if score <= 0:
            continue
        # Mild penalty for same-domain links — diversifies the frontier
        # so we don't recurse 4 levels deep into one site.
        if _domain_of(absolute) == page_domain:
            score *= 0.7
        link_candidates.append(
            _LinkCandidate(
                url=absolute,
                anchor=anchor,
                score=score,
                parent_url=page_url,
                depth=depth + 1,
            )
        )

    link_candidates.sort(key=lambda lc: lc.score, reverse=True)
    return passages, link_candidates[:_MAX_LINKS_PER_PAGE]


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def _dedupe_passages(passages: list[_Passage]) -> list[_Passage]:
    """Drop near-duplicates via shingle overlap.

    Two passages count as duplicates if their 5-word shingles share
    >= 60% by Jaccard overlap.  Highest-score wins; tie-breaks by
    earlier depth (closer to seed = more trustworthy).
    """
    passages = sorted(
        passages,
        key=lambda p: (p.score, -p.depth),
        reverse=True,
    )
    kept: list[_Passage] = []
    kept_shingles: list[frozenset[str]] = []
    for p in passages:
        sh = p.shingle()
        is_dup = False
        for existing in kept_shingles:
            if not sh or not existing:
                continue
            inter = len(sh & existing)
            if inter == 0:
                continue
            jac = inter / len(sh | existing)
            if jac >= 0.6:
                is_dup = True
                break
        if not is_dup:
            kept.append(p)
            kept_shingles.append(sh)
    return kept


def _build_envelope(
    state: _ResearchState,
    elapsed: float,
    stopped_reason: str,
    seed_results: list[dict],
) -> str:
    """Pack the final findings into a JSON envelope the calling LLM consumes."""
    deduped = _dedupe_passages(state.passages)

    # Sort by score; report top 40 to keep the response shape bounded.
    top_passages = sorted(deduped, key=lambda p: p.score, reverse=True)[:40]

    # Per-source rollup so the model can also see which pages contributed.
    by_source: dict[str, dict] = {}
    for p in top_passages:
        e = by_source.setdefault(
            p.source_url,
            {"url": p.source_url, "passage_count": 0, "max_score": 0.0, "depth": p.depth},
        )
        e["passage_count"] += 1
        e["max_score"] = max(e["max_score"], p.score)

    payload = {
        "query": state.query,
        "stats": {
            "elapsed_seconds": round(elapsed, 2),
            "pages_fetched": state.page_count,
            "passages_kept": len(top_passages),
            "passages_before_dedup": len(state.passages),
            "stopped_reason": stopped_reason,
        },
        "seed_results": seed_results,
        "passages": [
            {
                "text": p.text,
                "source_url": p.source_url,
                "depth": p.depth,
                "relevance": round(p.score, 4),
            }
            for p in top_passages
        ],
        "sources": sorted(
            by_source.values(),
            key=lambda e: e["max_score"],
            reverse=True,
        ),
        "fetch_errors": state.fetch_errors[:10],
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _do_research(
    query: str,
    max_depth: int,
    max_breadth: int,
    max_pages: int,
    max_seconds: int,
    num_seeds: int,
) -> str:
    state = _ResearchState(query=query, query_terms=_query_terms(query))
    if not query.strip():
        return _build_envelope(state, 0.0, "empty_query", [])

    fetch_sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30), follow_redirects=True
    ) as session:
        # --- Round 0: search seeds --------------------------------
        # Reuse the same SearxNG call path as the ``web_search`` tool
        # (tools/search.py::searx_query) — same timeout / blocklist /
        # filter logic, no duplication.
        try:
            raw_seeds = await searx_query(query, num_results=num_seeds)
        except SearxError as e:
            logger.warning("deep_research: seed search failed: %s", e)
            raw_seeds = []
        seeds: list[dict] = []
        for r in raw_seeds:
            url = r.get("url") or ""
            if not url or _is_blocklisted_url(url):
                continue
            seeds.append(
                {
                    "url": url,
                    "title": r.get("title") or "",
                    "snippet": r.get("content") or "",
                }
            )
        if not seeds:
            return _build_envelope(
                state,
                time.monotonic() - state.started_at,
                "no_seeds",
                [],
            )

        frontier: list[_LinkCandidate] = []
        for s in seeds:
            if s["url"] in state.visited:
                continue
            state.visited.add(s["url"])
            frontier.append(
                _LinkCandidate(
                    url=s["url"],
                    anchor=s.get("title") or "",
                    score=1.0,
                    parent_url="",
                    depth=0,
                )
            )

        async def fetch_and_extract(cand: _LinkCandidate) -> list[_LinkCandidate]:
            """Fetch one URL, extract passages + outbound links into state,
            and return the outbound link candidates for the next round.
            """
            if state.page_count >= max_pages:
                return []
            if time.monotonic() - state.started_at >= max_seconds:
                return []
            async with fetch_sem:
                # Reuse the same raw-HTML fetch path as the ``fetch_page``
                # tool (tools/fetch.py::fetch_html).  Deep research
                # skips the Playwright fallback path for budget reasons
                # — too slow across dozens of pages.
                html = await fetch_html(cand.url, client=session)
            if html is None:
                state.fetch_errors.append(
                    {"url": cand.url, "depth": cand.depth, "reason": "fetch_failed"}
                )
                return []
            state.page_count += 1
            passages, links = extract_passages_and_links(
                html, cand.url, state.query_terms, cand.depth
            )
            state.passages.extend(passages)
            return links

        # --- Round 1..max_depth: expand frontier ------------------
        stopped_reason = "depth_reached"
        for depth in range(0, max_depth + 1):
            if not frontier:
                stopped_reason = "frontier_empty"
                break
            if time.monotonic() - state.started_at >= max_seconds:
                stopped_reason = "time_budget"
                break
            if state.page_count >= max_pages:
                stopped_reason = "page_budget"
                break

            # Cap the per-round breadth.
            current = frontier[:max_breadth]
            frontier = []

            results = await asyncio.gather(
                *(fetch_and_extract(c) for c in current),
                return_exceptions=True,
            )

            # Collect outbound links from this round into the next
            # frontier, enforcing per-domain diversity.
            for r in results:
                if isinstance(r, BaseException):
                    continue
                for link in r:
                    if link.url in state.visited:
                        continue
                    if _is_blocklisted_url(link.url):
                        continue
                    domain = _domain_of(link.url)
                    if state.domain_counts.get(domain, 0) >= _MAX_LINKS_PER_DOMAIN:
                        continue
                    state.visited.add(link.url)
                    state.domain_counts[domain] = (
                        state.domain_counts.get(domain, 0) + 1
                    )
                    frontier.append(link)

            # Highest-scoring links first for the next round.
            frontier.sort(key=lambda lc: lc.score, reverse=True)

    elapsed = time.monotonic() - state.started_at
    return _build_envelope(state, elapsed, stopped_reason, seeds)


# ---------------------------------------------------------------------------
# Tool registration: this module exports ``deep_research`` as a plain
# coroutine.  ``server.py`` registers it with FastMCP via
# ``mcp.tool(name=..., description=...)(deep_research)`` — same explicit
# pattern as the other tools, so all four register at the same place.
# ---------------------------------------------------------------------------


# Description used by server.py when registering this function with
# FastMCP.  Kept as a module-level constant so server.py can read it
# without having to maintain two copies of the wording.
DEEP_RESEARCH_DESCRIPTION = (
    "Iteratively search the web and follow links to gather scored, "
    "structured findings about a topic.  Returns JSON with passages, "
    "their source URLs, the link graph that found them, and run "
    "statistics.  Long-running (typically 30-180s).  The calling LLM "
    "should synthesise the JSON into a written report — this tool "
    "intentionally does not write prose."
)


async def deep_research(
    query: str,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_breadth: int = _DEFAULT_MAX_BREADTH,
    max_pages: int = _DEFAULT_MAX_PAGES,
    max_seconds: int = _DEFAULT_MAX_SECONDS,
    num_seeds: int = _DEFAULT_NUM_SEEDS,
) -> str:
    """Conduct iterative deep-research on ``query``.

    Args:
        query: The research question.  Required.
        max_depth: Maximum BFS depth from seed results.  Default 2.
            ``0`` means "just fetch the search results, don't follow
            links."  ``3`` is the practical ceiling — beyond that the
            link relevance drops off fast.
        max_breadth: Maximum URLs fetched per round.  Default 6.
            Higher = better coverage but more wall-clock.
        max_pages: Hard cap on total pages fetched across all rounds.
            Default 30.  Stops the run early when reached.
        max_seconds: Hard wall-clock budget in seconds.  Default 180.
        num_seeds: Search results used as round-0 seeds.  Default 8.

    Returns:
        JSON-encoded envelope containing:
          - ``query``                : the original query
          - ``stats``                : page count, elapsed time, stop reason
          - ``seed_results``         : the original search results
          - ``passages``             : top-scored content snippets with
                                       source URL, depth, and relevance score
          - ``sources``              : per-URL rollup of passage counts
          - ``fetch_errors``         : URLs that failed to fetch (capped at 10)

    The JSON is intentionally lean — no narrative summary, no markdown.
    The calling LLM has full conversation context and is in a better
    position to write the report.
    """
    # Sanity-clamp the knobs to a reasonable range so a confused caller
    # can't accidentally request a 30-minute / 1000-page run.
    max_depth = max(0, min(max_depth, 4))
    max_breadth = max(1, min(max_breadth, 12))
    max_pages = max(1, min(max_pages, 60))
    max_seconds = max(10, min(max_seconds, 600))
    num_seeds = max(1, min(num_seeds, 20))

    started = time.monotonic()
    logger.info(
        "deep_research start",
        extra={
            "query": query,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "max_pages": max_pages,
            "max_seconds": max_seconds,
        },
    )
    try:
        result = await asyncio.wait_for(
            _do_research(
                query, max_depth, max_breadth, max_pages, max_seconds, num_seeds
            ),
            # Outer guard — twice max_seconds in case something pathological
            # blocks the cancellation path.
            timeout=max_seconds * 2,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        return json.dumps(
            {
                "query": query,
                "stats": {
                    "elapsed_seconds": round(elapsed, 2),
                    "stopped_reason": "hard_timeout",
                },
                "passages": [],
                "sources": [],
                "fetch_errors": [],
            }
        )
    logger.info(
        "deep_research done",
        extra={
            "query": query,
            "elapsed": round(time.monotonic() - started, 2),
        },
    )
    return result
