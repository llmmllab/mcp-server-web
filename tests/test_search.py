"""Tests for tools/search.py — the web_search tool."""

import json

import pytest
import respx
import httpx

from tools.search import web_search, _envelope
from config import SEARX_HOST, SEARCH_HARD_TIMEOUT


class TestEnvelope:
    def test_without_error(self):
        result = _envelope("test query", [{"title": "A", "url": "http://a.com"}])
        data = json.loads(result)
        assert data["query"] == "test query"
        assert data["results"] == data["contents"]
        assert "error" not in data

    def test_with_error(self):
        result = _envelope("", [], error="Something broke")
        data = json.loads(result)
        assert data["error"] == "Something broke"
        assert data["results"] == []

    def test_results_and_contents_are_equal(self):
        """``results`` and ``contents`` are envelope aliases — compare
        equal after a JSON round-trip.  Identity (``is``) can't survive
        ``json.dumps`` + ``json.loads`` since JSON has no concept of
        shared references."""
        items = [{"title": "X"}]
        data = json.loads(_envelope("q", items))
        assert data["results"] == data["contents"]


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self):
        result = await web_search("")
        data = json.loads(result)
        assert data["error"] == "Empty query"
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_whitespace_only_query_returns_error(self):
        result = await web_search("   ")
        data = json.loads(result)
        assert data["error"] == "Empty query"

    @respx.mock
    @pytest.mark.asyncio
    async def test_successful_search(self):
        mock_data = {
            "results": [
                {
                    "title": "Result One",
                    "url": "https://example.com/1",
                    "content": "First result snippet",
                },
                {
                    "title": "Result Two",
                    "url": "https://example.com/2",
                    "content": "Second result snippet",
                },
            ]
        }
        route = respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(200, json=mock_data))

        result = await web_search("python testing")
        assert route.called

        data = json.loads(result)
        assert data["query"] == "python testing"
        assert len(data["results"]) == 2
        assert data["results"][0]["title"] == "Result One"
        assert data["results"][0]["relevance"] == 1.0
        assert data["results"][1]["relevance"] == 0.95

    @respx.mock
    @pytest.mark.asyncio
    async def test_filters_robots_txt_urls(self):
        mock_data = {
            "results": [
                {"title": "Good", "url": "https://example.com/good", "content": "ok"},
                {"title": "Blocked", "url": "https://example.com/robots.txt", "content": "no"},
            ]
        }
        respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(200, json=mock_data))

        result = await web_search("test")
        data = json.loads(result)
        assert len(data["results"]) == 1
        assert data["results"][0]["title"] == "Good"

    @respx.mock
    @pytest.mark.asyncio
    async def test_filters_empty_urls(self):
        mock_data = {
            "results": [
                {"title": "No URL", "url": "", "content": "bad"},
                {"title": "Good", "url": "https://example.com/", "content": "ok"},
            ]
        }
        respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(200, json=mock_data))

        result = await web_search("test")
        data = json.loads(result)
        assert len(data["results"]) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_error_returns_structured_error(self):
        respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(500))

        result = await web_search("test")
        data = json.loads(result)
        assert "error" in data
        assert "500" in data["error"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_custom_num_results(self):
        mock_data = {
            "results": [
                {"title": f"R{i}", "url": f"https://example.com/{i}", "content": ""}
                for i in range(5)
            ]
        }
        respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(200, json=mock_data))

        result = await web_search("test", num_results=2)
        data = json.loads(result)
        assert len(data["results"]) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_custom_engines_and_categories(self):
        route = respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(200, json={"results": []}))

        await web_search("test", engines=["google"], categories=["news"])
        assert route.called
        params = dict(route.calls[0].request.url.params)
        assert params["engines"] == "google"
        assert params["categories"] == "news"

    @respx.mock
    @pytest.mark.asyncio
    async def test_custom_time_range(self):
        route = respx.get(f"{SEARX_HOST}/search").mock(return_value=httpx.Response(200, json={"results": []}))

        await web_search("test", time_range="week")
        params = dict(route.calls[0].request.url.params)
        assert params["time_range"] == "week"

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error_returns_envelope(self):
        respx.get(f"{SEARX_HOST}/search").mock(side_effect=httpx.ConnectError("connection refused"))

        result = await web_search("test")
        data = json.loads(result)
        assert "error" in data
        assert data["results"] == []
