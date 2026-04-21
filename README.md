# mcp-server-web

MCP server for web search and content fetching, using SearxNG for search and BeautifulSoup + Playwright for page content extraction.

## Tools

### `web_search`
Search the web via a SearxNG instance. Returns structured JSON results with titles, URLs, content snippets, and relevance scores.

- **query** (required): Search query string
- **num_results**: Number of results (default: 10)
- **categories**: Search categories (`general`, `news`, `science`, `it`, `shopping`, `images`, `videos`, `music`, `files`, `social`)
- **engines**: Override default SearxNG engines
- **time_range**: Filter by time (`day`, `week`, `month`, `year`)

### `fetch_page`
Fetch and extract readable text from a URL. Handles static HTML, SPAs (auto-detected, rendered via Playwright), plain text, markdown, and JSON.

- **url** (required): URL to fetch (http/https only)
- **render_js**: Force Playwright rendering (default: false)

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|---|---|---|
| `SEARX_HOST` | `http://localhost:8080` | SearxNG instance URL |
| `SEARX_ENGINES` | `google,bing,duckduckgo,startpage` | Comma-separated engines |
| `SEARX_MAX_RESULTS` | `10` | Default result count |
| `SEARX_LANGUAGE` | `en` | Search language |
| `SEARX_SAFESEARCH` | `1` | Safe search level (0/1/2) |
| `MAX_CONTENT_LENGTH` | `8000` | Max chars returned from fetch |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout in seconds |
| `MCP_TRANSPORT` | `http` | Transport: `http` or `stdio` |

## Development

```bash
make install      # Install dependencies
make start        # Run with stdio transport
make start-http   # Run with HTTP transport on port 8000
```

## Deployment

```bash
make deploy       # Build, push, and deploy to k8s
make logs         # Tail pod logs
make status       # Check pod status
```
