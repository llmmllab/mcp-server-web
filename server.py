"""
Server code for FastMCP Web Search & Fetch integration.
This server provides web search (via SearxNG) and web content fetching
(via BeautifulSoup + Playwright) as MCP tools.
"""

import sys

# The Dockerfile CMD is `python server.py`, which loads this file as
# `__main__`.  Our tools modules use `from server import mcp` to register
# their decorators — but that import resolves `server` as a *separate*
# module, which would re-execute the top-level code and create a SECOND
# FastMCP instance.  The decorators would then attach to that second
# instance while ``__main__.mcp.run(...)`` serves the first (empty) one,
# producing the symptom "tools/list returns []".  Aliasing the two module
# objects before any tools import keeps everyone on the same instance.
if __name__ == "__main__" and "server" not in sys.modules:
    sys.modules["server"] = sys.modules["__main__"]

from fastmcp import FastMCP

from config import MCP_TRANSPORT

mcp = FastMCP("web")

# Import tools to register them
import tools.search  # type: ignore  noqa: E402,F401
import tools.fetch  # type: ignore  noqa: E402,F401

if __name__ == "__main__":
    transport = MCP_TRANSPORT or "http"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
