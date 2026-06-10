"""Tests for tools/fetch.py — the fetch_page tool and HTML analysis."""

import re

import pytest
import respx
import httpx

from tools.fetch import fetch_page, _analyze_html
from config import MAX_CONTENT_LENGTH, SPA_TEXT_THRESHOLD, SPA_SCRIPT_RATIO


class TestAnalyzeHtml:
    def test_basic_text_extraction(self):
        # The paragraph must clear ``SPA_TEXT_THRESHOLD`` (default 50)
        # so the page isn't classified as SPA on the visible-text
        # signal — a tiny "Hello world" page legitimately looks like
        # an empty-shell SPA to the heuristic.
        html = (
            "<html><body><p>Hello world from a regular static page with "
            "more than enough body text to clear the SPA-detection "
            "threshold easily.</p></body></html>"
        )
        text, is_spa = _analyze_html(html)
        assert "Hello world" in text
        assert is_spa is False

    def test_strips_script_and_style(self):
        html = "<html><body><p>Real text</p><script>var x = 'hidden';</script><style>.x{}</style></body></html>"
        text, _ = _analyze_html(html)
        assert "Real text" in text
        assert "hidden" not in text

    def test_strips_nav_header_footer_aside(self):
        html = "<html><body><nav>skip me</nav><header>also skip</header><main>keep this</main><footer>bye</footer><aside>nope</aside></body></html>"
        text, _ = _analyze_html(html)
        assert "keep this" in text
        assert "skip me" not in text
        assert "also skip" not in text
        assert "bye" not in text
        assert "nope" not in text

    def test_detects_spa_by_low_text(self):
        html = f'<html><body><div id="root"></div><script type="module">console.log(1)</script></body></html>'
        text, is_spa = _analyze_html(html)
        assert is_spa is True

    def test_detects_spa_by_framework_marker(self):
        html = '<html><body><p>This is a sufficiently long paragraph of text that exceeds the threshold easily</p><script>window.__NUXT__ = {}</script></body></html>'
        _, is_spa = _analyze_html(html)
        assert is_spa is True

    def test_detects_spa_by_empty_root_div(self):
        html = '<html><body><div id="root"></div><p>enough text here to pass the threshold for a normal page easily</p></body></html>'
        _, is_spa = _analyze_html(html)
        assert is_spa is True

    def test_normal_page_not_spa(self):
        html = '<html><body><article><p>A properly rendered page with enough visible text to not be considered a single page application by any reasonable metric.</p></article></body></html>'
        _, is_spa = _analyze_html(html)
        assert is_spa is False

    def test_empty_html(self):
        text, is_spa = _analyze_html("")
        assert text == ""
        assert is_spa is True  # empty text is below threshold


class TestFetchPage:
    @pytest.mark.asyncio
    async def test_invalid_url_scheme(self):
        result = await fetch_page("ftp://example.com")
        assert "Error" in result
        assert "Invalid URL" in result

    @pytest.mark.asyncio
    async def test_no_scheme(self):
        result = await fetch_page("example.com")
        assert "Error" in result
        assert "Invalid URL" in result

    @pytest.mark.asyncio
    async def test_empty_url(self):
        result = await fetch_page("")
        assert "Error" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_html_page(self):
        html = "<html><body><h1>Title</h1><p>Page content here</p></body></html>"
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )

        result = await fetch_page("https://example.com/page")
        assert "Content from https://example.com/page" in result
        assert "Title" in result
        assert "Page content here" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_plain_text(self):
        respx.get("https://example.com/readme.txt").mock(
            return_value=httpx.Response(200, text="plain text content", headers={"content-type": "text/plain"})
        )

        result = await fetch_page("https://example.com/readme.txt")
        assert "Content from https://example.com/readme.txt" in result
        assert "plain text content" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_json(self):
        respx.get("https://example.com/data.json").mock(
            return_value=httpx.Response(200, text='{"key": "value"}', headers={"content-type": "application/json"})
        )

        result = await fetch_page("https://example.com/data.json")
        assert "Content from https://example.com/data.json" in result
        assert '{"key": "value"}' in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_error_status(self):
        respx.get("https://example.com/404").mock(return_value=httpx.Response(404))

        result = await fetch_page("https://example.com/404")
        assert "HTTP 404" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_binary_content_error(self):
        respx.get("https://example.com/image.png").mock(
            return_value=httpx.Response(200, text="binary", headers={"content-type": "image/png"})
        )

        result = await fetch_page("https://example.com/image.png")
        assert "Error" in result
        assert "image/png" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error(self):
        respx.get("https://nonexistent.example.com/").mock(side_effect=httpx.ConnectError("connection refused"))

        result = await fetch_page("https://nonexistent.example.com/")
        assert "Error" in result
        assert "Network error" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_small_page_output_unchanged(self):
        html = "<html><body><h1>Title</h1><p>Short content</p></body></html>"
        respx.get("https://example.com/small").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )
        result = await fetch_page("https://example.com/small")
        assert result.startswith("Content from https://example.com/small:\n\n")
        assert "[chars" not in result
        assert "More content available" not in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_pagination_first_page_has_offset_footer(self):
        long_html = "<html><body><p>" + "x" * (MAX_CONTENT_LENGTH + 5000) + "</p></body></html>"
        respx.get("https://example.com/long").mock(
            return_value=httpx.Response(200, text=long_html, headers={"content-type": "text/html"})
        )
        result = await fetch_page("https://example.com/long")
        assert "[chars 0-" in result
        assert "call fetch_page again with offset=" in result
        assert "[Content truncated due to length...]" not in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_pagination_offset_returns_remainder(self):
        head = "A" * MAX_CONTENT_LENGTH
        tail = "B" * 3000
        long_html = f"<html><body><p>{head}{tail}</p></body></html>"
        respx.get("https://example.com/long2").mock(
            return_value=httpx.Response(200, text=long_html, headers={"content-type": "text/html"})
        )
        page1 = await fetch_page("https://example.com/long2")
        m = re.search(r"offset=(\d+)", page1)
        assert m is not None
        next_off = int(m.group(1))
        page2 = await fetch_page("https://example.com/long2", offset=next_off)
        assert "B" in page2
        assert "[chars " in page2  # non-first page still shows the range header
        assert "call fetch_page again with offset=" not in page2  # last page

    @respx.mock
    @pytest.mark.asyncio
    async def test_offset_past_end_message(self):
        html = "<html><body><p>Some content here that is long enough.</p></body></html>"
        respx.get("https://example.com/short").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )
        result = await fetch_page("https://example.com/short", offset=99999)
        assert "past the end of content" in result
        assert "More content available" not in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_extracted_content(self):
        respx.get("https://example.com/empty").mock(
            return_value=httpx.Response(200, text="<html><body></body></html>", headers={"content-type": "text/html"})
        )
        result = await fetch_page("https://example.com/empty")
        assert result == "Content from https://example.com/empty:\n\n"
        assert "[chars" not in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_pagination_uses_cache_no_refetch(self):
        head = "A" * MAX_CONTENT_LENGTH
        tail = "B" * 3000
        long_html = f"<html><body><p>{head}{tail}</p></body></html>"
        route = respx.get("https://example.com/cached").mock(
            return_value=httpx.Response(200, text=long_html, headers={"content-type": "text/html"})
        )
        await fetch_page("https://example.com/cached")
        await fetch_page("https://example.com/cached", offset=MAX_CONTENT_LENGTH)
        assert route.call_count == 1
