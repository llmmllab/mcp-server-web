"""Integration tests for server.py — verify tools are registered and callable
via the FastMCP server."""

import json

import pytest

import respx
import httpx

from config import SEARX_HOST


@pytest.mark.asyncio
async def test_tools_are_discoverable():
    """Both tools must appear in tools/list."""
    from fastmcp import FastMCP
    from tools.fetch import fetch_page
    from tools.search import web_search

    mcp = FastMCP("test")
    mcp.tool(
        name="fetch_page",
        description="Fetch and extract readable text content from a web page URL. "
        "Handles static HTML, SPAs (via Playwright), plain text, and JSON."
    )(fetch_page)
    mcp.tool(
        name="web_search",
        description="Search the web using SearxNG. Returns structured results "
        "with titles, URLs, and content snippets."
    )(web_search)

    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]

    assert "fetch_page" in tool_names, f"fetch_page not found in {tool_names}"
    assert "web_search" in tool_names, f"web_search not found in {tool_names}"
    assert len(tools) == 2, f"Expected exactly 2 tools, got {len(tools)}"


@pytest.mark.asyncio
async def test_tool_descriptions_are_set():
    """Each tool must have a non-empty description."""
    from fastmcp import FastMCP
    from tools.fetch import fetch_page
    from tools.search import web_search

    mcp = FastMCP("test")
    mcp.tool(
        name="fetch_page",
        description="Fetch and extract readable text content from a web page URL. "
        "Handles static HTML, SPAs (via Playwright), plain text, and JSON."
    )(fetch_page)
    mcp.tool(
        name="web_search",
        description="Search the web using SearxNG. Returns structured results "
        "with titles, URLs, and content snippets."
    )(web_search)

    tools = await mcp.list_tools()
    for tool in tools:
        assert tool.description, f"Tool '{tool.name}' has no description"
        assert len(tool.description) > 10, f"Tool '{tool.name}' description too short"


@pytest.mark.asyncio
async def test_web_search_callable_via_server():
    """Call web_search through the MCP server and verify it returns valid JSON."""
    from fastmcp import FastMCP
    from tools.search import web_search

    mcp = FastMCP("test")
    mcp.tool(
        name="web_search",
        description="Search the web using SearxNG."
    )(web_search)

    # Empty query should short-circuit with an error envelope
    result = await mcp.call_tool(
        "web_search",
        arguments={"query": ""},
    )
    # FastMCP wraps the return value; extract the text content
    text = result.content[0].text if hasattr(result, "content") else str(result)
    data = json.loads(text)
    assert data["error"] == "Empty query"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_returns_results_via_server():
    """Full integration: call web_search via MCP server with mocked SearxNG."""
    from fastmcp import FastMCP
    from tools.search import web_search

    mock_data = {
        "results": [
            {"title": "Test Result", "url": "https://example.com/", "content": "snippet"}
        ]
    }
    respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(200, json=mock_data))

    mcp = FastMCP("test")
    mcp.tool(
        name="web_search",
        description="Search the web using SearxNG."
    )(web_search)

    result = await mcp.call_tool(
        "web_search",
        arguments={"query": "integration test"},
    )
    text = result.content[0].text if hasattr(result, "content") else str(result)
    data = json.loads(text)
    assert len(data["results"]) == 1
    assert data["results"][0]["title"] == "Test Result"


@pytest.mark.asyncio
async def test_fetch_page_callable_via_server():
    """Call fetch_page through the MCP server and verify URL validation works."""
    from fastmcp import FastMCP
    from tools.fetch import fetch_page

    mcp = FastMCP("test")
    mcp.tool(
        name="fetch_page",
        description="Fetch and extract readable text content from a web page URL."
    )(fetch_page)

    result = await mcp.call_tool(
        "fetch_page",
        arguments={"url": "ftp://invalid"},
    )
    text = result.content[0].text if hasattr(result, "content") else str(result)
    assert "Error" in text
    assert "Invalid URL" in text


@respx.mock
@pytest.mark.asyncio
async def test_fetch_page_returns_content_via_server():
    """Full integration: call fetch_page via MCP server with mocked HTTP."""
    from fastmcp import FastMCP
    from tools.fetch import fetch_page

    html = "<html><body><h1>Hello</h1><p>World</p></body></html>"
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
    )

    mcp = FastMCP("test")
    mcp.tool(
        name="fetch_page",
        description="Fetch and extract readable text content from a web page URL."
    )(fetch_page)

    result = await mcp.call_tool(
        "fetch_page",
        arguments={"url": "https://example.com/"},
    )
    text = result.content[0].text if hasattr(result, "content") else str(result)
    assert "Hello" in text
    assert "World" in text
