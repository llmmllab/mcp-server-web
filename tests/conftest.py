"""Shared test setup for the mcp-server-web tools.

The ``server`` module is the FastMCP registry — our tool modules import
``from server import mcp`` at import time.  Tests don't need a running
server, but the import has to succeed.  Aliasing ``__main__`` → ``server``
the same way :mod:`server` does at production startup keeps the
decorator-time tool registration happy.
"""

import os

# Disable Playwright's real-browser SPA fallback during tests: point the
# browser path at a nonexistent dir so chromium.launch() fails fast and
# _render_with_playwright returns None. This matches CI (no Chromium binary)
# and makes respx-mocked fetch tests deterministic instead of leaking to the
# live internet. setdefault so a dev who wants real browsers can override.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent-mcp-server-web-tests")

import sys
from pathlib import Path

# Ensure the project root is importable as the tools refer to
# ``from server import mcp`` and ``from config import ...``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest


@pytest.fixture(autouse=True)
def _clear_content_cache():
    """Each test starts with an empty fetch cache so a page cached by one test
    can't bleed into another (and so respx call-count assertions hold)."""
    from tools._content_cache import content_cache

    content_cache.clear()
    yield
    content_cache.clear()
