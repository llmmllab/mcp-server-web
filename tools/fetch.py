"""
Web content fetching tool using BeautifulSoup and Playwright.

Fetches and extracts readable text content from web pages, including:
- Static HTML pages via httpx + BeautifulSoup
- Plain text and markdown files
- Single Page Applications (SPA) via Playwright rendering

This module migrated from aiohttp to httpx (2026-05-29) so the test
suite can mock HTTP via respx — the dev dep ``respx`` only intercepts
httpx, not aiohttp.  The api repo also uses httpx so this aligns the
HTTP-client surface across both codebases.
"""

import asyncio
import hashlib
import logging
import re
from typing import Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from config import (
    BROWSER_HEADERS,
    FETCH_HARD_TIMEOUT,
    FRAMEWORK_MARKERS,
    MAX_CONTENT_LENGTH,
    REQUEST_TIMEOUT,
    SPA_TEXT_THRESHOLD,
    SPA_SCRIPT_RATIO,
)
from tools._content_cache import content_cache
from tools._pagination import window_text

logger = logging.getLogger("mcp-server-web.fetch")


# Classes / ids that almost always wrap boilerplate (sidebars, related
# posts, comment sections, share widgets, etc.).  Stripped before text
# extraction so we don't bleed the same headline into the article AND
# the "popular posts" widget — the dominant source of duplicate content
# the previous extraction emitted.
#
# The list is regex-matched against the concatenated class / id / role
# attributes of every tag, so ``<div class="related-articles">`` and
# ``<section role="complementary">`` both get pruned even though they
# use different HTML semantics.
_BOILERPLATE_CLASS_RE = re.compile(
    r"(?xi)\b(?:"
    r"sidebar|side-bar|side_panel|complementary"
    r"|related[-_]?(?:posts|articles|stories|reading|content)?"
    r"|comment(?:s|-section|-list)?"
    r"|popular(?:-posts)?|trending|recommended|most-read"
    r"|share|social|sharing|share-buttons?"
    r"|newsletter|subscribe(?:-box)?|signup|cta"
    r"|advert(?:isement)?|ad-[a-z]+|sponsored|promo"
    r"|cookie(?:-banner|-notice|-consent)?"
    r"|breadcrumb|pagination|paginator"
    r"|widget|sidebar-widget"
    r"|menu|nav(?:igation|bar)?"
    r"|footer|site-footer|page-footer"
    r"|header|site-header|page-header"
    r"|skip-link|screen-reader|sr-only|visually-hidden"
    r")\b"
)

# Block-level tags whose text contents become their own paragraph in
# the extracted output.  Preserving these boundaries is what stops the
# previous "everything joined with single spaces" behaviour that lost
# heading/paragraph structure and made the output unreadable.
_BLOCK_TAGS = (
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "article", "section", "pre", "tr",
)


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    """In-place: remove semantic + class/id-pattern boilerplate.

    Modern templates rarely use ``<aside>``/``<nav>`` for sidebars
    — they wrap them in ``<div class="sidebar">`` instead.  This pass
    walks every tag once, prunes anything whose class/id/role attributes
    match :data:`_BOILERPLATE_CLASS_RE`, and short-circuits the
    duplicate-content problem at the source.
    """
    for tag in soup(
        ["script", "style", "noscript", "iframe", "nav",
         "header", "footer", "aside", "form"]
    ):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        # An earlier ``decompose()`` in this same loop may have detached
        # a child of ``tag`` — bs4 still includes it in the snapshot
        # but its ``.attrs`` is now None, which would AttributeError
        # on ``.get()``.  Skip detached / attribute-less nodes.
        if getattr(tag, "attrs", None) is None:
            continue
        identifiers = " ".join(
            [
                " ".join(tag.get("class") or []),
                str(tag.get("id") or ""),
                str(tag.get("role") or ""),
            ]
        ).strip()
        if identifiers and _BOILERPLATE_CLASS_RE.search(identifiers):
            tag.decompose()


def _extract_blocks(soup: BeautifulSoup) -> list[str]:
    """Return the cleaned text of each block-level tag, in document order.

    Preserves paragraph / heading boundaries (each block is its own
    string in the returned list) and deduplicates adjacent/repeated
    blocks via a 120-char MD5 signature — the second occurrence of an
    excerpt that appears in both the main article and a "related posts"
    widget gets dropped without losing the first one.

    Only **leaf** block tags are extracted: a tag is skipped if any of
    its descendants is also a block tag.  This avoids the
    duplicate-content bug where an outer ``<article>`` returned the
    full concatenation of every nested ``<p>`` AND each ``<p>`` was
    *also* returned individually.
    """
    blocks: list[str] = []
    seen_sigs: set[str] = set()
    for tag in soup.find_all(_BLOCK_TAGS):
        # Skip non-leaf blocks (parent of another block tag).  Their
        # children will be processed individually in the same pass.
        if any(tag.find(child) is not None for child in _BLOCK_TAGS):
            continue
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        # Collapse internal whitespace but keep the block whole.
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        sig = hashlib.md5(text[:120].lower().encode("utf-8")).hexdigest()
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        blocks.append(text)
    return blocks


def _analyze_html(html_content: str) -> Tuple[str, bool]:
    """
    Parse HTML once. Return (cleaned_text, is_spa).

    SPA signals (evaluated BEFORE boilerplate stripping so we can still
    see framework markers and empty-root containers):
      - visible text length below threshold
      - script/style byte ratio above threshold
      - empty #root / #app / #vue-app / .app containers
      - framework markers in the raw HTML string

    Text extraction (after boilerplate stripping):
      - prune script/style/nav/header/footer/aside semantic tags
      - prune any tag whose class/id/role matches the boilerplate
        regex (sidebars, related posts, comments, share widgets, etc.)
      - extract block-level text as discrete paragraphs joined by
        ``\\n\\n`` so the calling model sees paragraph structure
      - drop adjacent-duplicate paragraphs (same text in main vs widget)
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # SPA detection runs on the pristine soup so we don't false-positive
    # after stripping boilerplate.
    script_text = "".join(
        tag.get_text() for tag in soup.find_all(["script", "style"])
    )
    script_ratio = (len(script_text) / len(html_content)) if html_content else 0.0
    empty_root = any(
        container is not None and not container.get_text(strip=True)
        for container in (
            soup.find("div", id="root"),
            soup.find("div", id="app"),
            soup.find("div", id="vue-app"),
            soup.find("div", class_="app"),
        )
    )
    has_framework_marker = any(
        marker in html_content for marker in FRAMEWORK_MARKERS
    )

    _strip_boilerplate(soup)
    blocks = _extract_blocks(soup)
    # Fallback: if block extraction yielded nothing (e.g. content lives
    # in nested divs without any of our recognised block tags), fall
    # back to the old whole-soup extraction so we don't return empty.
    if not blocks:
        raw_text = soup.get_text(" ", strip=True)
        clean_text = re.sub(r"\s+", " ", raw_text).strip()
    else:
        clean_text = "\n\n".join(blocks)

    is_spa = (
        len(clean_text.strip()) < SPA_TEXT_THRESHOLD
        or script_ratio > SPA_SCRIPT_RATIO
        or empty_root
        or has_framework_marker
    )
    return clean_text, is_spa


async def _render_with_playwright(url: str) -> Optional[str]:
    """Render a URL with Playwright; return HTML, or None if unavailable/failed."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)

            for selector in ("body", "main", "#root", ".app", "article"):
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    break
                except Exception:
                    continue

            html_content = await page.content()
            await browser.close()
            return html_content
    except Exception:
        return None


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
            f"[offset {offset} is past the end of content; {total} chars total]."
        )

    end = offset + win["returned_chars"]
    body = f"Content from {url} [chars {offset}-{end} of {total}]:\n\n{text}"
    if win["has_more"]:
        body += (
            f"\n\n[More content available — call fetch_page again with "
            f"offset={win['next_offset']} to continue.]"
        )
    return body


async def fetch_html(
    url: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Fetch raw HTML for ``url`` and return the body, or None on error.

    Reusable building block shared by :func:`fetch_page` (the MCP tool)
    and :func:`tools.deep_research.deep_research` (which needs raw HTML
    for link extraction, not the cleaned text).  Always returns within
    ``REQUEST_TIMEOUT`` seconds.

    Pass an ``httpx.AsyncClient`` to share connection pooling across
    many fetches (the deep-research BFS does this).  Without one, a
    per-call client is created and closed.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            follow_redirects=True,
            headers=BROWSER_HEADERS,
        )
    assert client is not None
    try:
        response = await client.get(url, headers=BROWSER_HEADERS)
        if response.status_code >= 400:
            return None
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None
        return response.text
    except (httpx.RequestError, asyncio.TimeoutError) as e:
        logger.debug("fetch_html failed for %s: %s", url, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("fetch_html unexpected error for %s: %s", url, e)
        return None
    finally:
        if owns_client:
            await client.aclose()


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
