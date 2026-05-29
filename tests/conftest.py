"""Shared test setup for the mcp-server-web tools.

The ``server`` module is the FastMCP registry — our tool modules import
``from server import mcp`` at import time.  Tests don't need a running
server, but the import has to succeed.  Aliasing ``__main__`` → ``server``
the same way :mod:`server` does at production startup keeps the
decorator-time tool registration happy.
"""

import sys
from pathlib import Path

# Ensure the project root is importable as the tools refer to
# ``from server import mcp`` and ``from config import ...``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
