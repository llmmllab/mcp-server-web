"""
Web search tool using SearxNG.

Performs web searches via a SearxNG instance and returns structured results
with titles, URLs, content snippets, and relevance scores.
"""

import json
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
)
from server import mcp


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
        JSON string with search results including title, url, content, and relevance.
    """
    if not query.strip():
        return json.dumps({"error": "Empty query", "results": []})

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

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{SEARX_HOST}/search",
                params=params,
                headers=BROWSER_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status >= 400:
                    return json.dumps(
                        {"error": f"SearxNG returned HTTP {response.status}", "results": []}
                    )
                data = await response.json()

        raw_results = data.get("results", [])
        results = []
        for i, r in enumerate(raw_results[:max_results]):
            url = r.get("url", "")
            if url.endswith("robots.txt"):
                continue
            results.append(
                {
                    "title": r.get("title", "No title"),
                    "url": url,
                    "content": r.get("content", ""),
                    "relevance": round(1.0 - (0.05 * i), 2),
                }
            )

        return json.dumps({"query": query, "results": results})

    except aiohttp.ClientError as e:
        return json.dumps({"error": f"Network error: {e}", "results": []})
    except Exception as e:
        return json.dumps({"error": f"Search failed: {e}", "results": []})
