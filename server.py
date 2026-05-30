"""
Server code for FastMCP Web Search & Fetch integration.

This server provides web search (via SearxNG), web content fetching
(via httpx + BeautifulSoup + Playwright), and iterative deep-research
as MCP tools.

Registration pattern: tool modules export plain coroutines without any
``@mcp.tool`` decorator, and this file binds each one to the FastMCP
instance via ``mcp.tool(name=..., description=...)(fn)``.  Keeping
registration centralised here avoids the circular-import problem the
decorator-in-module pattern caused (every tool file would have to do
``from server import mcp``) and makes the tool catalogue auditable
from one place.
"""

from fastmcp import FastMCP

from config import MCP_TRANSPORT
from tools.deep_research import DEEP_RESEARCH_DESCRIPTION, deep_research
from tools.fetch import fetch_page
from tools.fetch_with_links import (
    FETCH_WITH_LINKS_DESCRIPTION,
    fetch_with_links,
)
from tools.search import web_search

mcp = FastMCP("web")

# --- Tool registration -----------------------------------------------------
# Order doesn't matter for correctness — listed roughly by complexity:
# basic search, basic fetch, LLM-orchestrated research building block,
# one-shot deep-research orchestrator.

mcp.tool(
    name="web_search",
    description=(
        "Search the web using SearxNG. Returns structured results with titles, "
        "URLs, and content snippets."
    ),
)(web_search)

mcp.tool(
    name="fetch_page",
    description=(
        "Fetch and extract readable text content from a web page URL. "
        "Handles static HTML, SPAs (via Playwright), plain text, and JSON."
    ),
)(fetch_page)

mcp.tool(
    name="fetch_with_links",
    description=FETCH_WITH_LINKS_DESCRIPTION,
)(fetch_with_links)

mcp.tool(
    name="deep_research",
    description=DEEP_RESEARCH_DESCRIPTION,
)(deep_research)


if __name__ == "__main__":
    transport = MCP_TRANSPORT or "http"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host="0.0.0.0", port=8000)
