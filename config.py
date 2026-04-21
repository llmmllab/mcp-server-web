"""Configuration constants for Web MCP Server."""

import os
from pathlib import Path

# Base directory for this package
BASE_DIR = Path(__file__).parent

# Load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    env_path = BASE_DIR / ".env"
    load_dotenv(dotenv_path=env_path) if env_path.exists() else load_dotenv()
except ImportError:
    pass

# SearxNG configuration
SEARX_HOST = os.environ.get("SEARX_HOST", "http://localhost:8080")
SEARX_DEFAULT_ENGINES = os.environ.get(
    "SEARX_ENGINES", "google,bing,duckduckgo,startpage"
).split(",")
SEARX_DEFAULT_CATEGORIES = os.environ.get(
    "SEARX_CATEGORIES", "general"
).split(",")
SEARX_MAX_RESULTS = int(os.environ.get("SEARX_MAX_RESULTS", "10"))
SEARX_LANGUAGE = os.environ.get("SEARX_LANGUAGE", "en")
SEARX_SAFESEARCH = int(os.environ.get("SEARX_SAFESEARCH", "1"))
SEARX_TIME_RANGE = os.environ.get("SEARX_TIME_RANGE", "")

# Web reader configuration
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", "8000"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
SPA_TEXT_THRESHOLD = int(os.environ.get("SPA_TEXT_THRESHOLD", "50"))
SPA_SCRIPT_RATIO = float(os.environ.get("SPA_SCRIPT_RATIO", "0.5"))

# Server configuration
MCP_SERVER_BASE_URL = os.environ.get("MCP_SERVER_BASE_URL")
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "http")

# Browser headers for HTTP requests
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Framework markers for SPA detection
FRAMEWORK_MARKERS = (
    '<script type="module"',
    "window.__NUXT__",
    "__VUE__",
    "__reactInternalInstance",
    "webpackJsonp",
)
