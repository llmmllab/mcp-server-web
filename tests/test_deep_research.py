"""Tests for the deep-research helpers.

The full BFS loop with real HTTP is exercised by integration in
production; these unit tests cover the pure-Python pieces:

* scoring (passage relevance, link relevance, blocklist)
* domain helpers
* extract_passages_and_links over crafted HTML
* dedup / shingle
* URL blocklist patterns
"""

import pytest

from tools.deep_research import (
    _LinkCandidate,
    _Passage,
    _dedupe_passages,
    _domain_of,
    _is_blocklisted_url,
    _link_score,
    _passage_score,
    _query_terms,
    extract_passages_and_links,
)


class TestScoring:
    def test_passage_zero_for_no_overlap(self):
        terms = _query_terms("how does python async work")
        assert _passage_score("the cat sat on the mat", terms) == 0.0

    def test_passage_higher_for_more_overlap(self):
        terms = _query_terms("python async event loop")
        low = _passage_score(
            "Python is a programming language used for many things.", terms
        )
        high = _passage_score(
            "Python's async event loop schedules coroutines that yield control.",
            terms,
        )
        assert high > low > 0

    def test_link_score_rewards_anchor_match_over_path_match(self):
        terms = _query_terms("python async tutorial")
        path_only = _link_score(
            anchor="Click here",
            url="https://example.com/python/async/tutorial.html",
            query_terms=terms,
        )
        anchor_match = _link_score(
            anchor="Python async tutorial",
            url="https://example.com/x/y/z.html",
            query_terms=terms,
        )
        assert anchor_match > path_only > 0


class TestDomainAndBlocklist:
    def test_domain_strips_www(self):
        assert _domain_of("https://www.example.com/foo") == "example.com"
        assert _domain_of("https://blog.example.com/x") == "blog.example.com"

    def test_blocklist_catches_socials(self):
        for url in [
            "https://facebook.com/share?u=foo",
            "https://twitter.com/intent/tweet?text=x",
            "https://www.linkedin.com/sharing/share-offsite",
            "https://example.com/login",
            "https://example.com/cart/checkout",
            "https://example.com/file.pdf",
            "https://example.com/image.png",
        ]:
            assert _is_blocklisted_url(url), f"should block {url!r}"

    def test_blocklist_lets_articles_through(self):
        for url in [
            "https://realpython.com/python-async-features/",
            "https://docs.python.org/3/library/asyncio.html",
            "https://example.com/blog/2024/why-async-matters",
        ]:
            assert not _is_blocklisted_url(url), f"shouldn't block {url!r}"


class TestExtractPassagesAndLinks:
    def test_keeps_query_relevant_passages(self):
        html = """
        <html><body><article>
          <p>The async event loop in Python schedules coroutines and
             dispatches I/O readiness via select on Unix or IOCP on Windows.</p>
          <p>Bananas are yellow and grow on trees in tropical climates worldwide.</p>
          <p>Python's asyncio module provides the event-loop runtime that
             makes coroutine scheduling efficient for I/O-bound workloads.</p>
        </article></body></html>
        """
        terms = _query_terms("python async event loop")
        passages, _links = extract_passages_and_links(
            html, "https://example.com/post", terms, depth=0
        )
        passage_texts = [p.text for p in passages]
        # Relevant ones present.
        assert any("event loop" in t for t in passage_texts)
        assert any("asyncio module" in t for t in passage_texts)
        # Banana paragraph either filtered (zero score) or ranked below
        # the relevant ones.  We require it to be absent.
        assert not any("Bananas" in t for t in passage_texts)

    def test_extracts_scored_links(self):
        html = """
        <html><body><article>
          <p>Some content here so the page has body text passing the
             minimum-length filter for paragraphs.</p>
          <a href="https://example.com/python/async-deep-dive">Python async deep dive</a>
          <a href="https://example.com/recipes/banana-bread">Banana bread recipe</a>
          <a href="https://facebook.com/share">Share on Facebook</a>
        </article></body></html>
        """
        terms = _query_terms("python async tutorial")
        _passages, links = extract_passages_and_links(
            html, "https://example.com/post", terms, depth=0
        )
        urls = [lc.url for lc in links]
        # Relevant link kept; irrelevant link rejected (score 0);
        # Facebook share blocklisted.
        assert "https://example.com/python/async-deep-dive" in urls
        assert "https://example.com/recipes/banana-bread" not in urls
        assert not any("facebook.com" in u for u in urls)


class TestDedupePassages:
    def test_jaccard_drops_near_duplicates(self):
        p1 = _Passage(
            text=(
                "The Global Interpreter Lock prevents true parallel execution "
                "in CPython by serialising bytecode evaluation across threads."
            ),
            source_url="a",
            score=1.0,
            depth=0,
        )
        # Same text, different source — should dedupe.
        p2 = _Passage(text=p1.text, source_url="b", score=0.9, depth=1)
        # Genuinely different — should survive.
        p3 = _Passage(
            text=(
                "Async event loops use selectors to dispatch I/O readiness "
                "events and resume waiting coroutines without blocking."
            ),
            source_url="c",
            score=0.8,
            depth=0,
        )
        kept = _dedupe_passages([p1, p2, p3])
        assert len(kept) == 2
        # Highest-score copy of the dup pair survives.
        assert any(p.source_url == "a" for p in kept)
        assert not any(p.source_url == "b" for p in kept)


class TestLinkCandidateDataclass:
    def test_frozen_construction(self):
        lc = _LinkCandidate(
            url="https://example.com",
            anchor="Example",
            score=1.5,
            parent_url="https://parent.com",
            depth=1,
        )
        assert lc.url == "https://example.com"
        with pytest.raises(Exception):
            lc.score = 2.0  # frozen
