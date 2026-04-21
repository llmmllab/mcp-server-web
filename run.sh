#!/bin/bash
# Wrapper script to run the Web MCP server with stdio transport
export MCP_TRANSPORT=stdio
cd "$(dirname "$0")"
uv run python server.py
