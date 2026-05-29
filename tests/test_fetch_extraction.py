"""Tests for the boilerplate-strip + block-extraction overhaul in tools/fetch.py.

The bug report behind this rewrite: extracted page content was including
the same headlines/excerpts twice (once from the main article, once from
the sidebar's "popular posts" widget) and was losing all paragraph
structure (everything joined with single spaces).
"""

from bs4 import BeautifulSoup

from tools.fetch import (
    _BOILERPLATE_CLASS_RE,
    _analyze_html,
    _extract_blocks,
    _strip_boilerplate,
)


class TestStripBoilerplate:
    def test_removes_semantic_tags(self):
        html = """
        <html><body>
          <header>site header</header>
          <nav>nav links</nav>
          <main><p>real article body</p></main>
          <aside>sidebar widget</aside>
          <footer>footer junk</footer>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _strip_boilerplate(soup)
        text = soup.get_text(" ", strip=True)
        assert "real article body" in text
        assert "site header" not in text
        assert "nav links" not in text
        assert "sidebar widget" not in text
        assert "footer junk" not in text

    def test_removes_class_matched_blocks(self):
        html = """
        <html><body>
          <article>main article body about the topic</article>
          <div class="sidebar">sidebar widget content</div>
          <div class="related-posts">other articles you may like</div>
          <div class="comments">user comments here</div>
          <div class="share-buttons">tweet this article</div>
          <div class="newsletter-signup">subscribe to our list</div>
          <div class="cookie-banner">we use cookies</div>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _strip_boilerplate(soup)
        text = soup.get_text(" ", strip=True)
        assert "main article body about the topic" in text
        for boilerplate in (
            "sidebar widget content",
            "other articles you may like",
            "user comments here",
            "tweet this article",
            "subscribe to our list",
            "we use cookies",
        ):
            assert boilerplate not in text, f"expected boilerplate dropped: {boilerplate!r}"

    def test_removes_id_matched_blocks(self):
        html = """
        <html><body>
          <article>real content</article>
          <div id="sidebar">side junk</div>
          <div id="comments">comment junk</div>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _strip_boilerplate(soup)
        text = soup.get_text(" ", strip=True)
        assert "real content" in text
        assert "side junk" not in text
        assert "comment junk" not in text


class TestExtractBlocks:
    def test_returns_paragraphs_as_discrete_strings(self):
        html = """
        <article>
          <h1>The Title</h1>
          <p>First paragraph with enough content to be considered.</p>
          <p>Second distinct paragraph with different content here.</p>
        </article>
        """
        soup = BeautifulSoup(html, "html.parser")
        _strip_boilerplate(soup)
        blocks = _extract_blocks(soup)
        assert any("The Title" in b for b in blocks)
        assert any("First paragraph" in b for b in blocks)
        assert any("Second distinct paragraph" in b for b in blocks)
        # Title and first paragraph are NOT joined into one block.
        joined_into_one = any(
            "The Title" in b and "First paragraph" in b for b in blocks
        )
        assert not joined_into_one

    def test_deduplicates_repeated_blocks(self):
        """Same article excerpt rendered in main + 'popular posts' widget
        should appear once after extraction."""
        html = """
        <article>
          <h1>How GIL Works in Python</h1>
          <p>The Global Interpreter Lock prevents true parallelism in CPython.</p>
        </article>
        <div class="related">
          <h3>How GIL Works in Python</h3>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        # NB: don't strip boilerplate here so we can isolate the dedup.
        # (In production both strip and dedup run; either one alone is
        # enough to kill this duplicate, but we want dedup tested
        # independently.)
        blocks = _extract_blocks(soup)
        # Both copies share a >120-char-prefix MD5 signature → second dropped.
        title_hits = sum(1 for b in blocks if "How GIL Works in Python" in b)
        assert title_hits == 1, f"expected 1 occurrence, saw {title_hits}: {blocks!r}"


class TestAnalyzeHtml:
    def test_preserves_paragraph_structure_with_newlines(self):
        """Final ``clean_text`` should have ``\\n\\n`` between blocks
        instead of collapsing everything into a single space-joined
        string the way the previous extraction did.
        """
        html = """
        <html><body><article>
          <h1>Headline Goes Here</h1>
          <p>The first paragraph has enough content to be considered as a real block of text.</p>
          <p>The second paragraph has different content from the first and is also long enough.</p>
        </article></body></html>
        """
        text, is_spa = _analyze_html(html)
        assert not is_spa
        assert "\n\n" in text  # paragraph separator preserved
        # Order is preserved.
        assert text.index("Headline Goes Here") < text.index("first paragraph")
        assert text.index("first paragraph") < text.index("second paragraph")

    def test_drops_duplicate_widget_excerpts(self):
        """End-to-end version of the dedup test — pass real-world-shaped
        HTML through ``_analyze_html`` and confirm the article excerpt
        isn't duplicated by the sidebar widget."""
        html = """
        <html><body>
          <article>
            <h1>How Async Works in Python</h1>
            <p>Coroutines let you write asynchronous code that looks
               synchronous, yielding to the event loop on every await.</p>
          </article>
          <div class="sidebar">
            <div class="popular-posts">
              <h3>How Async Works in Python</h3>
              <p>Coroutines let you write asynchronous code that looks
                 synchronous, yielding to the event loop on every await.</p>
            </div>
          </div>
        </body></html>
        """
        text, _ = _analyze_html(html)
        # The article appears once.
        title_count = text.count("How Async Works in Python")
        assert title_count == 1, f"expected 1 title, saw {title_count}"
        excerpt_count = text.count("Coroutines let you write")
        assert excerpt_count == 1, f"expected 1 excerpt, saw {excerpt_count}"


def test_boilerplate_regex_catches_modern_class_patterns():
    """Spot-check the regex against the patterns most blogs use."""
    patterns_that_should_match = [
        "sidebar",
        "side-bar",
        "related-articles",
        "related-posts",
        "comments-section",
        "popular-posts",
        "share-buttons",
        "social-share",
        "newsletter-signup",
        "cookie-banner",
        "page-footer",
        "site-header",
        "skip-link",
        "screen-reader-text",
        "sr-only",
    ]
    for p in patterns_that_should_match:
        assert _BOILERPLATE_CLASS_RE.search(p), f"regex missed {p!r}"

    patterns_that_should_NOT_match = [
        "article-body",
        "main-content",
        "post-body",
        "entry-content",
    ]
    for p in patterns_that_should_NOT_match:
        assert not _BOILERPLATE_CLASS_RE.search(p), f"regex false-positive on {p!r}"
