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

import aiohttp

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
from server import mcp

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


@mcp.tool(
    name="web_search",
    description="Search the web using SearxNG. Returns structured results with titles, URLs, and content snippets.",
)
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
    if not query.strip():
        return _envelope(query, [], error="Empty query")

    max_results = num_results or SEARX_MAX_RESULTS
    search_engines = engines or SEARX_DEFAULT_ENGINES

    params = {
        "q": query,
        "format": "json",
        "language": SEARX_LANGUAGE,
        "safesearch": str(SEARX_SAFESEARCH),
        "engines": ",".join(search_engines),
        "categories": ",".join(categories),
    }
    tr = time_range or SEARX_TIME_RANGE
    if tr:
        params["time_range"] = tr

    async def _do_request() -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{SEARX_HOST}/search",
                params=params,
                headers=BROWSER_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status >= 400:
                    return {"_error": f"SearxNG returned HTTP {response.status}"}
                return await response.json()

    try:
        data = await asyncio.wait_for(_do_request(), timeout=SEARCH_HARD_TIMEOUT)
    except asyncio.TimeoutError:
        msg = f"Search timed out after {SEARCH_HARD_TIMEOUT}s"
        logger.warning(msg, extra={"query": query})
        return _envelope(query, [], error=msg)
    except aiohttp.ClientError as e:
        msg = f"Network error: {e}"
        logger.warning(msg, extra={"query": query})
        return _envelope(query, [], error=msg)
    except Exception as e:  # pragma: no cover — defensive
        msg = f"Search failed: {e}"
        logger.error(msg, extra={"query": query})
        return _envelope(query, [], error=msg)

    if "_error" in data:
        return _envelope(query, [], error=data["_error"])

    raw_results = data.get("results", [])
    contents: list[dict] = []
    for i, r in enumerate(raw_results[:max_results]):
        url = r.get("url", "")
        if not url or url.endswith("robots.txt"):
            continue
        contents.append(
            {
                "title": r.get("title", "No title"),
                "url": url,
                "content": r.get("content", ""),
                "relevance": round(1.0 - (0.05 * i), 2),
            }
        )

    logger.info(
        "web_search completed",
        extra={"query": query, "result_count": len(contents)},
    )
    return _envelope(query, contents)
