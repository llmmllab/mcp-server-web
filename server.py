"""
Server code for FastMCP Web Search & Fetch integration.
This server provides web search (via SearxNG) and web content fetching
(via BeautifulSoup + Playwright) as MCP tools.
"""

from fastmcp import FastMCP

from config import MCP_TRANSPORT
from tools.fetch import fetch_page
from tools.search import web_search

mcp = FastMCP("web")

# Import tools to register them
import tools.search  # type: ignore  noqa: E402,F401
import tools.fetch  # type: ignore  noqa: E402,F401
import tools.fetch_with_links  # type: ignore  noqa: E402,F401
import tools.deep_research  # type: ignore  noqa: E402,F401

if __name__ == "__main__":
    transport = MCP_TRANSPORT or "http"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host="0.0.0.0", port=8000)
