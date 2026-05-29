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

The extractor strips semantic boilerplate (`<nav>`, `<header>`, `<footer>`, `<aside>`, `<form>`) *plus* any tag whose `class` / `id` / `role` matches a boilerplate pattern (`sidebar`, `related-posts`, `comments`, `share-buttons`, `newsletter-signup`, `cookie-banner`, etc.). It preserves paragraph/heading boundaries (each block becomes its own `\n\n`-separated entry instead of collapsing everything to one space-joined string) and deduplicates adjacent/repeated blocks via a 120-char prefix MD5 — so the same headline appearing in the main article AND a "popular posts" widget shows up only once.

### `fetch_with_links` — the LLM-orchestrated research building block
Fetch a web page and return both its cleaned text AND its outbound links, with anchor text and (when a `query` is supplied) a relevance score.  Pair with `web_search` for a turn-by-turn research loop the model drives natively — no server-side heuristic in the loop.

- **url** (required): The page to fetch (http/https only)
- **query**: Optional research question; when set, outbound links are scored and sorted by anchor + URL-path overlap with the query terms (content is not filtered — the model is in a better position to decide what's relevant once it has read the page)
- **max_links**: Maximum outbound links returned (default 20, clamped to [1, 60])

Returns JSON: `{"url", "content", "links": [{"url", "anchor", "same_domain", "relevance"?}, ...]}`.

Recommended LLM-driven pattern:
1. `web_search(query)` → seed URLs
2. `fetch_with_links(url, query=query)` on a promising seed → text + scored outbound links
3. The model picks the most useful link from `links` based on what it just read
4. `fetch_with_links(next_url, query=query)` → repeat
5. The model synthesises a report from accumulated content when it has enough

The model decides depth, breadth, and stopping — no heuristic to fight.

### `deep_research` — one-shot shortcut
Iteratively search the web and follow links to gather scored, structured findings about a topic. Returns JSON with passages, source URLs, the link graph that produced them, and run statistics. **Long-running** (typically 30-180s). The calling LLM should synthesise the JSON into a written report — this tool intentionally does not write prose.

Use this when round-trip token cost matters more than fine-grained control.  For exploratory research where the model wants to redirect mid-stream, prefer `fetch_with_links`.

- **query** (required): The research question
- **max_depth**: BFS depth from the seed search (default 2, clamped to [0, 4])
- **max_breadth**: URLs fetched per round (default 6, clamped to [1, 12])
- **max_pages**: Hard cap on total pages (default 30, clamped to [1, 60])
- **max_seconds**: Wall-clock budget (default 180, clamped to [10, 600])
- **num_seeds**: Search results used as round-0 seeds (default 8, clamped to [1, 20])

Strategy:
1. **Round 0**: SearxNG query → top-N seed URLs (reuses `web_search`'s SearxNG path).
2. **Round 1..max_depth**: fetch each frontier URL concurrently (reuses `fetch_page`'s raw-HTML path), extract leaf-block paragraphs scored by query-term density, and harvest outbound `<a href>` links scored by anchor-text + URL-path overlap.
3. **Frontier promotion**: top-K scored links advance to the next round, filtered for URL-blocklist (social/login/file types), per-domain caps (source diversity), and visited-URL dedup.
4. **Termination**: whichever hits first — `max_depth`, `max_pages`, `max_seconds`, or empty frontier.
5. **Synthesis**: shingle-based dedup across pages (Jaccard ≥ 60% on 5-grams), sort by relevance, return top 40 passages with citations.

Synthesised from public deep-research designs (OpenAI's Plan-Act-Observe loop, GPT Researcher's planner-executor with question decomposition, and qx-labs IterativeResearcher's Knowledge-Gap pattern), adapted for an MCP server with no LLM client of its own — the "intelligence" is heuristic and structural; the calling LLM does the reasoning.

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
