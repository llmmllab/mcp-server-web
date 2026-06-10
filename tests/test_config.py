"""Tests for config.py — verify defaults and env-var overrides."""

import os
from importlib import reload

import pytest


class TestConfigDefaults:
    """Verify config values fall back to sensible defaults."""

    def test_sphinx_defaults(self, monkeypatch):
        # Strip all config-related env vars to exercise defaults
        for key in list(os.environ.keys()):
            if key.startswith(("SEARX_", "MAX_CONTENT_", "REQUEST_", "MCP_",
                               "SPA_", "SEARCH_HARD", "FETCH_HARD")):
                monkeypatch.delenv(key, raising=False)
        import config  # noqa: F401
        reload(config)

        assert config.SEARX_HOST == "http://localhost:8080"
        assert config.SEARX_DEFAULT_ENGINES == [
            "google", "bing", "duckduckgo", "startpage"
        ]
        assert config.SEARX_MAX_RESULTS == 10
        assert config.SEARX_LANGUAGE == "en"
        assert config.SEARX_SAFESEARCH == 1
        assert config.SEARX_TIME_RANGE == ""
        assert config.MAX_CONTENT_LENGTH == 20000
        assert config.REQUEST_TIMEOUT == 30
        assert config.SPA_TEXT_THRESHOLD == 50
        assert config.SPA_SCRIPT_RATIO == 0.5
        assert config.SEARCH_HARD_TIMEOUT == 35
        assert config.FETCH_HARD_TIMEOUT == 75
        assert config.MCP_TRANSPORT == "http"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SEARX_HOST", "http://custom:9999")
        monkeypatch.setenv("SEARX_MAX_RESULTS", "25")
        monkeypatch.setenv("MAX_CONTENT_LENGTH", "15000")
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        monkeypatch.setenv("SEARCH_HARD_TIMEOUT", "60")
        monkeypatch.setenv("FETCH_HARD_TIMEOUT", "120")

        import config  # noqa: F401
        reload(config)

        assert config.SEARX_HOST == "http://custom:9999"
        assert config.SEARX_MAX_RESULTS == 25
        assert config.MAX_CONTENT_LENGTH == 15000
        assert config.MCP_TRANSPORT == "stdio"
        assert config.SEARCH_HARD_TIMEOUT == 60
        assert config.FETCH_HARD_TIMEOUT == 120

    def test_browser_headers_present(self):
        import config  # noqa: F401
        assert "User-Agent" in config.BROWSER_HEADERS
        assert "Accept" in config.BROWSER_HEADERS

    def test_framework_markers_non_empty(self):
        import config  # noqa: F401
        assert len(config.FRAMEWORK_MARKERS) > 0

    def test_content_cache_and_embedding_defaults(self, monkeypatch):
        for key in list(os.environ.keys()):
            if key.startswith(("CONTENT_CACHE_", "EMBEDDING_")):
                monkeypatch.delenv(key, raising=False)
        import config  # noqa: F401
        reload(config)

        assert config.CONTENT_CACHE_TTL == 300.0
        assert config.CONTENT_CACHE_MAX_ENTRIES == 64
        assert config.EMBEDDING_ENDPOINT == ""
        assert config.EMBEDDING_MODEL == ""
        assert config.EMBEDDING_API_KEY == ""
        assert config.EMBEDDING_TIMEOUT == 10.0

    def test_content_cache_and_embedding_env_overrides(self, monkeypatch):
        monkeypatch.setenv("CONTENT_CACHE_TTL", "60")
        monkeypatch.setenv("CONTENT_CACHE_MAX_ENTRIES", "8")
        monkeypatch.setenv("EMBEDDING_ENDPOINT", "http://emb/v1")
        monkeypatch.setenv("EMBEDDING_MODEL", "bge")
        monkeypatch.setenv("EMBEDDING_TIMEOUT", "3")
        import config  # noqa: F401
        reload(config)

        assert config.CONTENT_CACHE_TTL == 60.0
        assert config.CONTENT_CACHE_MAX_ENTRIES == 8
        assert config.EMBEDDING_ENDPOINT == "http://emb/v1"
        assert config.EMBEDDING_MODEL == "bge"
        assert config.EMBEDDING_TIMEOUT == 3.0
