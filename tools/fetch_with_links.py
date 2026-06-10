"""
Fetch + link-graph tool — the building block for LLM-orchestrated research.

Background
----------
``fetch_page`` returns cleaned text only, which is great for "show me what
this article says" but useless when the model wants to *decide what to read
next* based on what the page links to.  ``deep_research`` papers over that
by running a heuristic-driven BFS server-side — but heuristics are fragile,
and the calling LLM is generally a better judge of "which of these links
is worth following next" than any regex we could write.

This tool returns both halves:

  • the cleaned text content of the page (same extraction as ``fetch_page``)
  • the outbound links, with anchor text and (optionally) a relevance score
    against a ``query`` the caller supplies

The intended use pattern is back-and-forth turn-by-turn:

  1. LLM calls ``web_search(query)`` → seed URLs
  2. LLM calls ``fetch_with_links(url, query=query)`` on a promising seed
     → page text + scored outbound links
  3. LLM decides which link to read next based on the content it just saw
  4. LLM calls ``fetch_with_links(next_url, query=query)`` → repeat
  5. LLM synthesises a report from accumulated content

No heuristic is "burned in" — the model orchestrates, the tools just
fetch and surface structure.  ``deep_research`` (the one-shot tool) is
kept as a power-user shortcut for cases where round-trip token cost
matters more than fine-grained control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from config import FETCH_HARD_TIMEOUT, MAX_CONTENT_LENGTH
from tools.fetch import (
    _analyze_html,
    fetch_html,
)

# These helpers (URL blocklist + scoring + tokenisation) are duplicated
# inline rather than imported from :mod:`tools.deep_research` to avoid a
# circular import: ``server.py`` imports tool modules in order, and
# ``deep_research`` imports from ``server`` for the ``mcp`` registry.
# If ``fetch_with_links`` imported from ``deep_research``, the chain
# would deadlock on the partially-initialised ``deep_research`` module.
# Pulling the pieces inline is the smallest fix; a future refactor
# could extract them into a third ``tools/_research_helpers.py``.

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


def _is_blocklisted_url(url: str) -> bool:
    return bool(_URL_BLOCKLIST_PATTERNS.search(url))


def _normalize_terms(text: str) -> list[str]:
    return [
        w for w in re.findall(r"\w+", text.lower())
        if w not in _STOPWORDS and len(w) > 1
    ]


def _query_terms(query: str) -> frozenset[str]:
    return frozenset(_normalize_terms(query))


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
    return anchor_hits * 1.0 + path_hits * 0.4

logger = logging.getLogger("mcp-server-web.fetch_with_links")


# Maximum links surfaced in the response.  Going beyond ~30 floods the
# model's context with nav cruft; tighter than ~10 hides genuinely
# useful links on link-heavy pages (Wikipedia, reference docs).
_DEFAULT_MAX_LINKS = 20
_HARD_MAX_LINKS = 60

# Anchor text must be at least this long — pure-icon links ("→", "▶")
# and 1-char "x" close buttons aren't useful research targets.
_MIN_ANCHOR_CHARS = 2

# Hard cap on anchor length — repeated paragraph-as-anchor patterns
# bloat the response.
_MAX_ANCHOR_CHARS = 200


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


# Description used by server.py when registering this function with FastMCP.
FETCH_WITH_LINKS_DESCRIPTION = (
    "Fetch a web page and return BOTH its cleaned text AND its outbound "
    "links (with anchor text and optional relevance score against a "
    "query).  The intended pattern is turn-by-turn LLM-driven research: "
    "fetch a page, decide which link to follow next based on what you "
    "just read, fetch that, repeat.  Pass ``query`` to score links by "
    "relevance; omit it for raw link-graph output."
)


async def fetch_with_links(
    url: str,
    query: Optional[str] = None,
    max_links: int = _DEFAULT_MAX_LINKS,
) -> str:
    """Fetch ``url`` and return text content + outbound links as JSON.

    Args:
        url: The page to fetch (http:// or https:// only).
        query: Optional research question.  When set, outbound links
            are scored by anchor-text + URL-path overlap with the
            query terms and the response sorts links by descending
            score.  The text content itself is NOT filtered by
            ``query`` — the calling LLM is in a better position to
            decide what's relevant once it has read the full page.
        max_links: Maximum links returned (capped at ``_HARD_MAX_LINKS``).
            Defaults to 20 — enough to give the model good choice
            without flooding its context with nav cruft.

    Returns:
        JSON envelope ``{"url", "content", "links": [...], "error"?}``.
        Each link dict has ``url``, ``anchor``, ``same_domain``, and
        (if ``query`` was set) ``relevance``.
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

    try:
        html = await asyncio.wait_for(
            fetch_html(url), timeout=FETCH_HARD_TIMEOUT
        )
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

    # Reuse fetch_page's full analyze pipeline (boilerplate strip,
    # block extraction, dedup) — same extraction quality as the
    # standalone fetch_page tool.
    content, _is_spa = _analyze_html(html)
    if len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH] + "\n\n[Content truncated due to length...]"

    links = _extract_outbound_links(html, url, query)[:max_links]

    logger.info(
        "fetch_with_links completed",
        extra={
            "url": url,
            "query": query,
            "content_chars": len(content),
            "links_returned": len(links),
        },
    )
    return json.dumps(
        {
            "url": url,
            "content": content,
            "links": links,
        }
    )
