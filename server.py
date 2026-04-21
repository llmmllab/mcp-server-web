"""
Server code for FastMCP Web Search & Fetch integration.
This server provides web search (via SearxNG) and web content fetching
(via BeautifulSoup + Playwright) as MCP tools.
"""

from fastmcp import FastMCP

from config import MCP_TRANSPORT

mcp = FastMCP("web")

# Import tools to register them
import tools.search  # type: ignore  noqa: F401
import tools.fetch  # type: ignore  noqa: F401

if __name__ == "__main__":
    transport = MCP_TRANSPORT or "http"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
