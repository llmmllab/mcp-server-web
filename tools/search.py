"""
Web search tool using SearxNG.

Performs web searches via a SearxNG instance and returns structured results
with titles, URLs, content snippets, and relevance scores.

Reliability features (2026-05-22 audit alignment with llmmllab-api):
  - Hard ``asyncio.wait_for`` safety net on top of aiohttp's ClientTimeout
    (aiohttp can hang on some DNS/TLS failure modes).
  - Drops robots.txt URLs.
  - JSON envelope includes both ``results`` (legacy MCP key) and
    ``contents`` (the canonical key llmmllab-api consumes) so existing
    clients keep working while the API can read its preferred shape.
  - Empty/whitespace queries short-circuit with a structured error.
  - All error paths return a JSON-decodable envelope, never raise.
"""

import asyncio
import json
import logging
from typing import Literal

import httpx

from config import (
    SEARX_HOST,
    SEARX_DEFAULT_ENGINES,
    SEARX_MAX_RESULTS,
    SEARX_LANGUAGE,
    SEARX_SAFESEARCH,
    SEARX_TIME_RANGE,
    BROWSER_HEADERS,
    SEARCH_HARD_TIMEOUT,
)

logger = logging.getLogger("mcp-server-web.search")


def _envelope(
    query: str,
    contents: list[dict],
    *,
    error: str | None = None,
) -> str:
    """Build the canonical search response.

    Includes both ``results`` (legacy) and ``contents`` (canonical key
    used by llmmllab-api's ``_format_search_result``).  Either key holds
    the same list; downstreams can use whichever they prefer.
    """
    payload: dict = {"query": query, "results": contents, "contents": contents}
    if error:
        payload["error"] = error
    return json.dumps(payload)


async def web_search(
    query: str,
    num_results: int | None = None,
    categories: list[
        Literal[
            "general",
            "news",
            "science",
            "it",
            "shopping",
            "images",
            "videos",
            "music",
            "files",
            "social",
        ]
    ] = ["general"],
    engines: list[str] | None = None,
    time_range: str | None = None,
) -> str:
    """
    Search the web for information using multiple search engines via SearxNG.

    Args:
        query: The search query to execute.
        num_results: Number of results to return (default from config).
        categories: Search categories to include.
        engines: Specific SearxNG engines to use (overrides default).
        time_range: Time range filter (e.g. "day", "week", "month", "year").

    Returns:
        JSON string with shape::

            {"query": str, "results": [...], "contents": [...], "error"?: str}

        where each result has ``title``, ``url``, ``content``, ``relevance``.
    """
    return await _run_search(
        query,
        num_results=num_results,
        categories=categories,
        engines=engines,
        time_range=time_range,
    )


# ---------------------------------------------------------------------------
# Reusable helpers — public so other tools (e.g. ``deep_research``) can
# share the SearxNG call path without re-implementing the timeout /
# filter / envelope logic.
# ---------------------------------------------------------------------------


class SearxError(Exception):
    """Raised by :func:`searx_query` for any non-recoverable failure.

    The MCP tool wrapper catches this and folds it into the JSON
    envelope; :func:`deep_research` lets it propagate so it can choose
    its own failure shape (an envelope with ``stopped_reason="no_seeds"``).
    """


async def searx_query(
    query: str,
    *,
    num_results: int | None = None,
    categories: list[str] | None = None,
    engines: list[str] | None = None,
    time_range: str | None = None,
) -> list[dict]:
    """Issue a SearxNG search and return the filtered result list.

    Shared between :func:`web_search` (the MCP tool) and
    :func:`tools.deep_research.deep_research` so both go through the
    same timeout / blocklist / scoring logic.  Returns an empty list
    on error; raises :class:`SearxError` only when the caller has
    asked for one (the MCP tool prefers an envelope to an exception).

    Each result dict has: ``title``, ``url``, ``content``, ``relevance``.
    """
    if not query.strip():
        return []

    max_results = num_results or SEARX_MAX_RESULTS
    search_engines = engines or SEARX_DEFAULT_ENGINES
    search_categories = categories or ["general"]

    params = {
        "q": query,
        "format": "json",
        "language": SEARX_LANGUAGE,
        "safesearch": str(SEARX_SAFESEARCH),
        "engines": ",".join(search_engines),
        "categories": ",".join(search_categories),
    }
    tr = time_range or SEARX_TIME_RANGE
    if tr:
        params["time_range"] = tr

    async def _do_request() -> dict:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            response = await client.get(
                f"{SEARX_HOST}/search",
                params=params,
                headers=BROWSER_HEADERS,
            )
            if response.status_code >= 400:
                return {"_error": f"SearxNG returned HTTP {response.status_code}"}
            return response.json()

    try:
        data = await asyncio.wait_for(_do_request(), timeout=SEARCH_HARD_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "searx_query timed out after %ss", SEARCH_HARD_TIMEOUT,
            extra={"query": query},
        )
        raise SearxError(f"Search timed out after {SEARCH_HARD_TIMEOUT}s") from None
    except httpx.RequestError as e:
        logger.warning("searx_query network error: %s", e, extra={"query": query})
        raise SearxError(f"Network error: {e}") from e
    except Exception as e:  # pragma: no cover — defensive
        logger.error("searx_query failed: %s", e, extra={"query": query})
        raise SearxError(f"Search failed: {e}") from e

    if "_error" in data:
        logger.warning("searx_query upstream error: %s", data["_error"])
        raise SearxError(str(data["_error"]))

    contents: list[dict] = []
    for i, r in enumerate((data.get("results") or [])[:max_results]):
        url = r.get("url") or ""
        if not url or url.endswith("robots.txt"):
            continue
        contents.append(
            {
                "title": r.get("title") or "No title",
                "url": url,
                "content": r.get("content") or "",
                "relevance": round(1.0 - (0.05 * i), 2),
            }
        )
    return contents


async def _run_search(
    query: str,
    *,
    num_results: int | None,
    categories: list[str],
    engines: list[str] | None,
    time_range: str | None,
) -> str:
    """Implementation of the MCP ``web_search`` tool — thin envelope wrapper
    around :func:`searx_query`.
    """
    if not query.strip():
        return _envelope(query, [], error="Empty query")
    try:
        contents = await searx_query(
            query,
            num_results=num_results,
            categories=categories,
            engines=engines,
            time_range=time_range,
        )
    except SearxError as e:  # pragma: no cover — searx_query swallows by default
        return _envelope(query, [], error=str(e))
    logger.info(
        "web_search completed",
        extra={"query": query, "result_count": len(contents)},
    )
    return _envelope(query, contents)
