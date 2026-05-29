"""Tests for the LLM-orchestrated ``fetch_with_links`` tool."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from tools.fetch_with_links import (
    _extract_outbound_links,
    fetch_with_links,
)


class TestExtractOutboundLinks:
    def test_strips_fragments_and_dedupes(self):
        html = """
        <html><body><article>
          <a href="https://example.com/a">Article A</a>
          <a href="https://example.com/a#section-2">Article A — Section 2</a>
          <a href="https://example.com/a">Article A again</a>
          <a href="https://example.com/b">Article B</a>
        </article></body></html>
        """
        links = _extract_outbound_links(html, "https://source.com/", query=None)
        urls = [l["url"] for l in links]
        # ``/a`` and ``/a#section-2`` collapse to the same URL — dedupe wins.
        assert urls.count("https://example.com/a") == 1
        assert "https://example.com/b" in urls

    def test_filters_blocklisted_and_invalid(self):
        html = """
        <html><body>
          <a href="https://facebook.com/share">Share on FB</a>
          <a href="mailto:foo@bar.com">Email</a>
          <a href="javascript:void(0)">JS link</a>
          <a href="#top">Top</a>
          <a href="">Empty</a>
          <a href="https://example.com/article.pdf">PDF</a>
          <a href="https://example.com/login">Login</a>
          <a href="https://example.com/real-article">Real article here</a>
        </body></html>
        """
        links = _extract_outbound_links(html, "https://source.com/", query=None)
        urls = [l["url"] for l in links]
        assert urls == ["https://example.com/real-article"]

    def test_anchor_length_filter(self):
        html = """
        <html><body>
          <a href="https://example.com/a">→</a>
          <a href="https://example.com/b">x</a>
          <a href="https://example.com/c">Genuinely useful anchor text here</a>
        </body></html>
        """
        links = _extract_outbound_links(html, "https://source.com/", query=None)
        urls = [l["url"] for l in links]
        assert urls == ["https://example.com/c"]

    def test_scoring_when_query_given(self):
        html = """
        <html><body>
          <a href="https://example.com/python-async">Python async tutorial</a>
          <a href="https://example.com/banana-bread">Banana bread recipe</a>
          <a href="https://example.com/general">General page</a>
        </body></html>
        """
        links = _extract_outbound_links(
            html, "https://source.com/", query="python async"
        )
        # All three returned (no zero-score exclusion in this tool —
        # the calling LLM gets to see what's available), but the
        # relevant one ranks first.
        assert links[0]["url"] == "https://example.com/python-async"
        assert links[0]["relevance"] > 0
        # Lower-scored entries follow.
        scores = [l.get("relevance", 0) for l in links]
        assert scores == sorted(scores, reverse=True)

    def test_no_relevance_field_when_query_omitted(self):
        html = '<html><body><a href="https://example.com/a">A link</a></body></html>'
        links = _extract_outbound_links(html, "https://source.com/", query=None)
        assert len(links) == 1
        assert "relevance" not in links[0]
        assert links[0]["url"] == "https://example.com/a"

    def test_same_domain_flag(self):
        html = """
        <html><body>
          <a href="https://source.com/internal">Internal link</a>
          <a href="https://other.com/external">External link</a>
        </body></html>
        """
        links = _extract_outbound_links(html, "https://source.com/post", query=None)
        by_url = {l["url"]: l for l in links}
        assert by_url["https://source.com/internal"]["same_domain"] is True
        assert by_url["https://other.com/external"]["same_domain"] is False

    def test_strips_www_when_comparing_domains(self):
        html = '<a href="https://www.source.com/x">Same site</a>'
        links = _extract_outbound_links(html, "https://source.com/post", query=None)
        assert links[0]["same_domain"] is True


@pytest.mark.asyncio
class TestFetchWithLinks:
    async def test_returns_invalid_url_error(self):
        result = json.loads(await fetch_with_links("ftp://example.com"))
        assert "error" in result
        assert "http/https" in result["error"].lower()
        assert result["content"] == ""
        assert result["links"] == []

    async def test_returns_fetch_error_when_html_none(self):
        with patch(
            "tools.fetch_with_links.fetch_html",
            new=AsyncMock(return_value=None),
        ):
            result = json.loads(
                await fetch_with_links("https://example.com/missing")
            )
        assert "error" in result
        assert result["content"] == ""

    async def test_returns_content_and_scored_links(self):
        html = """
        <html><body><article>
          <h1>How Python Async Works</h1>
          <p>The async event loop in Python schedules coroutines and
             dispatches I/O readiness via select on Unix or IOCP on Windows.</p>
          <a href="https://example.com/async-event-loop">Python async event loop deep dive</a>
          <a href="https://example.com/threading">Threading vs locks</a>
          <a href="https://example.com/unrelated">Unrelated topic</a>
        </article></body></html>
        """
        with patch(
            "tools.fetch_with_links.fetch_html",
            new=AsyncMock(return_value=html),
        ):
            result = json.loads(
                await fetch_with_links(
                    "https://example.com/article", query="python async event loop"
                )
            )
        assert "How Python Async Works" in result["content"]
        urls = [l["url"] for l in result["links"]]
        assert "https://example.com/async-event-loop" in urls
        # The link whose anchor + URL path contain ``python``, ``async``,
        # ``event``, ``loop`` ranks first.  All three returned (no
        # zero-score exclusion — the LLM picks).
        assert result["links"][0]["url"] == "https://example.com/async-event-loop"
        assert result["links"][0]["relevance"] > 0

    async def test_max_links_caps_output(self):
        # 50 links → ask for only 5.
        anchors = "".join(
            f'<a href="https://example.com/p{i}">Page {i} about python</a>'
            for i in range(50)
        )
        html = f"<html><body>{anchors}</body></html>"
        with patch(
            "tools.fetch_with_links.fetch_html",
            new=AsyncMock(return_value=html),
        ):
            result = json.loads(
                await fetch_with_links(
                    "https://example.com/", query="python", max_links=5
                )
            )
        assert len(result["links"]) == 5
