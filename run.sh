#!/bin/bash
# Wrapper script to run the Web MCP server with stdio transport.
# Use the system Python (deps are installed via `uv pip install --system .` in
# the image / `uv pip install --system .` locally). NOT `uv run`, which would
# re-sync a fresh .venv on every invocation.
export MCP_TRANSPORT=stdio
cd "$(dirname "$0")"
exec python server.py
