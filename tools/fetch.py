"""
Web content fetching tool using BeautifulSoup and Playwright.

Fetches and extracts readable text content from web pages, including:
- Static HTML pages via aiohttp + BeautifulSoup
- Plain text and markdown files
- Single Page Applications (SPA) via Playwright rendering
"""

import asyncio
import logging
from typing import Optional, Tuple

import aiohttp
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
from server import mcp

logger = logging.getLogger("mcp-server-web.fetch")


def _analyze_html(html_content: str) -> Tuple[str, bool]:
    """
    Parse HTML once. Return (cleaned_text, is_spa).

    SPA signals:
      - visible text length below threshold
      - script/style byte ratio above threshold
      - empty #root / #app / #vue-app / .app containers
      - framework markers in the raw HTML string
    """
    soup = BeautifulSoup(html_content, "html.parser")

    script_text = "".join(tag.get_text() for tag in soup.find_all(["script", "style"]))
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
    has_framework_marker = any(marker in html_content for marker in FRAMEWORK_MARKERS)

    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    raw_text = soup.get_text()
    lines = (line.strip() for line in raw_text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    clean_text = " ".join(chunk for chunk in chunks if chunk)

    is_spa = (
        len(clean_text.strip()) < SPA_TEXT_THRESHOLD
        or script_ratio > SPA_SCRIPT_RATIO
        or empty_root
        or has_framework_marker
    )
    return clean_text, is_spa


def _truncate(text: str) -> str:
    if len(text) > MAX_CONTENT_LENGTH:
        return text[:MAX_CONTENT_LENGTH] + "\n\n[Content truncated due to length...]"
    return text


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


async def _process_html(url: str, html_content: str, allow_spa_fallback: bool) -> str:
    """Extract text from HTML; optionally fall back to Playwright on SPA pages."""
    text_content, is_spa = _analyze_html(html_content)

    if allow_spa_fallback and is_spa:
        rendered = await _render_with_playwright(url)
        if rendered:
            text_content, _ = _analyze_html(rendered)

    text_content = _truncate(text_content)
    return f"Content from {url}:\n\n{text_content}"


async def _fetch_impl(url: str, render_js: bool) -> str:
    """Inner fetch implementation; wrapped in ``asyncio.wait_for`` below."""
    if render_js:
        rendered = await _render_with_playwright(url)
        if not rendered:
            return "Error: Playwright rendering failed or not installed."
        return await _process_html(url, rendered, allow_spa_fallback=False)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    ) as session:
        async with session.get(
            url, headers=BROWSER_HEADERS, allow_redirects=True
        ) as response:
            if response.status >= 400:
                return f"Error: HTTP {response.status} when accessing {url}"

            content_type = response.headers.get("content-type", "").lower()
            body = await response.text()

            if "text/html" in content_type:
                return await _process_html(url, body, allow_spa_fallback=True)

            if "application/json" in content_type or "text/" in content_type:
                return f"Content from {url}:\n\n{_truncate(body)}"

            return (
                f"Error: URL does not appear to contain readable text "
                f"(content-type: {content_type})"
            )


@mcp.tool(
    name="fetch_page",
    description="Fetch and extract readable text content from a web page URL. Handles static HTML, SPAs (via Playwright), plain text, and JSON.",
)
async def fetch_page(url: str, render_js: bool = False) -> str:
    """
    Read and extract text content from a web page URL.

    Handles HTML (static and SPA), plain text/markdown, JSON, and other
    text-based content. Auto-detects JavaScript-rendered pages and can
    fall back to Playwright rendering.

    Reliability: a hard ``asyncio.wait_for`` wraps the entire fetch
    (``FETCH_HARD_TIMEOUT`` seconds, default 75) on top of aiohttp's
    internal ClientTimeout — aiohttp can hang on some DNS/TLS failure
    modes and Playwright can spin past its 30s timeout while waiting on
    selectors.  The outer wait_for guarantees a return.

    Args:
        url: The URL to read content from (must be http:// or https://).
        render_js: If True, skip aiohttp and render with Playwright directly.

    Returns:
        Clean text content from the web page, or error message if fetch fails.
    """
    try:
        parsed_url = urlparse(url)
        if not parsed_url.scheme or parsed_url.scheme not in ("http", "https"):
            return f"Error: Invalid URL '{url}'. Only HTTP and HTTPS URLs are supported."
    except Exception as e:
        return f"Error: Invalid URL format '{url}': {str(e)}"

    try:
        result = await asyncio.wait_for(
            _fetch_impl(url, render_js), timeout=FETCH_HARD_TIMEOUT
        )
        logger.info(
            "fetch_page completed",
            extra={
                "url": url,
                "render_js": render_js,
                "result_bytes": len(result),
            },
        )
        return result
    except asyncio.TimeoutError:
        msg = (
            f"Error: Timeout when trying to access {url} "
            f"({FETCH_HARD_TIMEOUT} seconds, hard cap)"
        )
        logger.warning(msg, extra={"url": url})
        return msg
    except aiohttp.ClientError as e:
        msg = f"Error: Network error when accessing {url}: {str(e)}"
        logger.warning(msg, extra={"url": url})
        return msg
    except Exception as e:
        msg = f"Error: Failed to read content from {url}: {str(e)}"
        logger.error(msg, extra={"url": url})
        return msg
