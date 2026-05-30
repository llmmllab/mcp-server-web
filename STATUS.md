# mcp-server-web — work-in-progress status

Captures the multi-session work on this repo through 2026-05-30: the
research-tool feature set, the HTTP-client migration, the GitHub
Actions runner fix, and what still needs verification / wrap-up.

## What landed

### Research tooling (4 MCP tools)

| Tool | Purpose | Drives the loop |
|---|---|---|
| `web_search` | SearxNG query → titles + URLs + snippets | LLM |
| `fetch_page` | URL → cleaned text (with Playwright fallback for SPAs) | LLM |
| `fetch_with_links` | URL → cleaned text **plus scored outbound links** | LLM (the building block for turn-by-turn research) |
| `deep_research` | Full server-side iterative BFS: search → fetch → score → follow links → synthesise | Server-side heuristic (one-shot shortcut) |

Strategy for `deep_research` was synthesised from public deep-research
designs:

- [OpenAI Deep Research](https://blog.promptlayer.com/how-deep-research-works/) — Plan-Act-Observe / ReAct loop
- [GPT Researcher](https://github.com/assafelovic/gpt-researcher) — planner→executor with question decomposition, 20+ sources
- [qx-labs IterativeResearcher](https://github.com/qx-labs/agents-deep-research) — Knowledge-Gap → Tool Selector → Observations → Writer

Adapted for an MCP server with no LLM client of its own: the
"intelligence" in `deep_research` is heuristic + structural; the
calling LLM does the actual synthesis. `fetch_with_links` is the
LLM-orchestrated alternative — the model drives the loop itself,
which avoids the heuristic fragility entirely at the cost of more
round-trip tokens.

### Fetch-extraction overhaul

The pre-existing `_analyze_html` had two symptoms:

1. **Block boundaries were lost** — `soup.get_text()` collapsed everything into one space-joined string, so headings / paragraphs / list items were indistinguishable to the calling model.
2. **Boilerplate-class blocks survived** — only semantic tags (`<nav>`, `<aside>`, etc.) were pruned; modern templates wrap sidebars / related-posts / comments / share widgets in `<div class="sidebar">` style markup. Their content (often a copy of the article excerpt) leaked into the output, producing "duplicate content" the model couldn't distinguish from the real article.

Fixes:

- `_strip_boilerplate` prunes any tag whose `class` / `id` / `role` matches a ~20-pattern boilerplate regex on top of the existing semantic-tag strip.
- `_extract_blocks` walks only **leaf** block tags (an outer `<article>` containing `<p>`s is skipped — only the `<p>`s are returned) and dedupes by 120-char-prefix MD5.
- `_analyze_html` now joins blocks with `\n\n` so paragraph structure survives.

Both `fetch_page` and `fetch_with_links` use the same extraction
pipeline; `deep_research` imports the helpers too.

### HTTP-client migration: aiohttp → httpx

The tools previously used aiohttp; tests added in the rebased main
mocked HTTP via `respx` (which only intercepts httpx). The whole
test surface was bypassing the mock and hitting real DNS, then
failing on the non-existent `aiohttp.Response` symbol.

Migration is now complete in `tools/fetch.py`, `tools/search.py`,
and `tools/deep_research.py`:

- `httpx.AsyncClient(timeout=..., follow_redirects=True)` for all fetches
- `response.status_code` / `response.text` (no longer the async `text()`)
- `httpx.RequestError` for network-error catches
- `respx`-based test mocks now actually intercept

This also aligns the HTTP-client choice with `llmmllab-api`, which
uses httpx throughout. One coherent network stack across both repos.

### Explicit MCP-tool registration

The rebased main started a migration away from the `@mcp.tool` decorator
pattern (which forced every tool module to `from server import mcp`,
creating a circular-import hazard). I finished the migration:

- Tool modules export plain coroutines and a `*_DESCRIPTION` constant.
- `server.py` registers each tool with FastMCP at one site:
  ```python
  mcp.tool(name=..., description=...)(fn)
  ```
- Single source of truth for the public tool catalogue.

### Test suite

`tests/` now has 73 passing unit tests covering:

- **Fetch extraction** (`test_fetch.py`, `test_fetch_extraction.py`):
  boilerplate strip (semantic + class + id), block extraction
  (leaf-only, dedup, paragraph preservation), SPA detection, full
  `_analyze_html` flow, truncation.
- **Deep research** (`test_deep_research.py`): passage / link scoring,
  domain helpers, URL blocklist, `extract_passages_and_links` over
  crafted HTML, shingle dedup.
- **fetch_with_links** (`test_fetch_with_links.py`): link extraction
  (fragment strip, dedup, blocklist, anchor-length, scoring,
  same-domain flag), tool wrapper (invalid URL, fetch failure, scored
  links, `max_links` cap).
- **Search** (`test_search.py`): envelope shape, custom params,
  HTTP-error → envelope-with-error path, network-error → envelope.
- **Server** (`test_server.py`): tools discoverable via FastMCP,
  descriptions present, end-to-end call via `mcp.call_tool`.
- **Config** (`test_config.py`): defaults, env-var overrides.

CI runs the full suite on every push / PR.

### GitHub Actions self-hosted runner registration

Root cause of "deploy stuck queued forever": the self-hosted runner
`lsnode-3` was registered **per repo** at
`llmmllab/llmmllab-runner`, `llmmllab/llmmllab-openclaw-k8s`, and
`LongStoryMedia/llmmllab-api` — but never at
`llmmllab/mcp-server-web`. Every `runs-on: self-hosted` job here
queued forever waiting for a runner that didn't exist on this repo.

Fixed by installing a new runner service on lsnode-3:

```
/opt/gh-runner-mcp-web/                                                     # install path
actions.runner.llmmllab-mcp-server-web.lsnode-3-mcp-web.service             # systemd unit
```

Cloned from the `/opt/gh-runner/` template, `.runner` + `.credentials`
scrubbed, registered with `config.sh --url
https://github.com/llmmllab/mcp-server-web --token <reg> --name
lsnode-3-mcp-web --labels self-hosted`, installed as systemd via
`svc.sh install lsm`, started, status `online`.

Queued jobs immediately started picking up.

## Outstanding verifications

### 1. Confirm the new image is in the live pod

```bash
# Once the green-build deploy completes:
POD=$(kubectl get pods -n llmmllab -l app=mcp-server-web -o jsonpath='{.items[0].metadata.name}')
kubectl describe pod -n llmmllab "$POD" | grep -E 'Image:|Started'
kubectl exec -n llmmllab "$POD" -- ls /app/tools/
#   expected: deep_research.py  fetch.py  fetch_with_links.py  search.py
```

### 2. Confirm tools are registered with the MCP server

The simplest live discovery probe — JSON-RPC over the streamable
HTTP transport. The server is on `mcp-server-web.llmmllab.svc.cluster.local:8000`
inside the cluster; port-forward or exec to hit it from your laptop.

```bash
# From inside the cluster (any pod with curl):
POD=$(kubectl get pods -n llmmllab -l app=mcp-server-web -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n llmmllab "$POD" -- sh -c '
  curl -sS -X POST http://localhost:8000/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '"'"'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'"'"'
' | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(t['name'] for t in d['result']['tools']))"
#   expected, in some order: web_search  fetch_page  fetch_with_links  deep_research
```

### 3. Smoke-test `fetch_with_links` end-to-end

Call the tool directly via JSON-RPC, with a real (small) public
article and a query, then eyeball the response shape:

```bash
POD=$(kubectl get pods -n llmmllab -l app=mcp-server-web -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n llmmllab "$POD" -- sh -c '
  curl -sS -X POST http://localhost:8000/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '"'"'{
      "jsonrpc": "2.0",
      "id": 2,
      "method": "tools/call",
      "params": {
        "name": "fetch_with_links",
        "arguments": {
          "url": "https://en.wikipedia.org/wiki/Asyncio",
          "query": "asyncio event loop coroutine",
          "max_links": 8
        }
      }
    }'"'"'
' | python3 -c "
import json, sys
d = json.load(sys.stdin)
payload = json.loads(d['result']['content'][0]['text'])
print(f\"url:           {payload['url']}\")
print(f\"content chars: {len(payload['content'])}\")
print(f\"first 200:     {payload['content'][:200]!r}\")
print(f\"links:\")
for lk in payload['links'][:5]:
    rel = lk.get('relevance', '-')
    print(f\"  ({rel}) {lk['url']}\")
    print(f\"        \\\"{lk['anchor'][:80]}\\\"\")
"
```

Pass criteria:

- `content` is a non-empty string with `\n\n` between paragraphs
- No duplicated sidebar / "related-posts" blocks (visually scan the
  first ~500 chars — if you see the article title or first
  paragraph repeated, the boilerplate strip missed something)
- `links` is a list of 5-8 entries, each with `url`, `anchor`,
  `same_domain`, `relevance`
- Sorted descending by `relevance`; the top link's anchor text
  should plausibly relate to the query

### 4. Smoke-test `deep_research` end-to-end

Long-running call (~30-180s).  Run it with a tight budget first so
you don't wait 3 minutes to see a failure:

```bash
POD=$(kubectl get pods -n llmmllab -l app=mcp-server-web -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n llmmllab "$POD" -- sh -c '
  curl -sS --max-time 240 -X POST http://localhost:8000/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '"'"'{
      "jsonrpc": "2.0",
      "id": 3,
      "method": "tools/call",
      "params": {
        "name": "deep_research",
        "arguments": {
          "query": "how does Pythons asyncio event loop scheduler work",
          "max_depth": 1,
          "max_breadth": 4,
          "max_pages": 8,
          "max_seconds": 60,
          "num_seeds": 5
        }
      }
    }'"'"'
' | python3 -c "
import json, sys
d = json.load(sys.stdin)
payload = json.loads(d['result']['content'][0]['text'])
print(f\"query:         {payload['query']}\")
print(f\"stats:         {payload['stats']}\")
print(f\"seed_results:  {len(payload['seed_results'])} URLs\")
print(f\"sources kept:  {len(payload['sources'])}\")
print(f\"passages kept: {len(payload['passages'])}\")
if payload['fetch_errors']:
    print(f\"fetch errors:  {len(payload['fetch_errors'])}\")
print()
print('Top 3 passages:')
for p in payload['passages'][:3]:
    print(f\"  [rel={p['relevance']} depth={p['depth']}] {p['source_url']}\")
    print(f\"    {p['text'][:200]!r}\")
"
```

Pass criteria:

- `stats.stopped_reason` is one of `frontier_empty`, `depth_reached`,
  `page_budget`, `time_budget` (the run terminated cleanly)
- `stats.pages_fetched` is 1-8 (within budget)
- `passages` is a non-empty list of `{text, source_url, depth, relevance}`
  dicts, sorted by `relevance` desc
- No duplicate passages across `source_url`s — the shingle dedup
  should catch obvious copy-paste between sources

Failure cases worth flagging:

- `stats.stopped_reason == "no_seeds"` → SearxNG is unreachable
  from inside the pod. Check `SEARX_HOST` and the searxng pod's
  status.
- All `fetch_errors`, zero passages → either every seed returned
  non-HTML content or every page failed to fetch. Inspect
  `fetch_errors[*].reason`.
- Run hangs past `max_seconds` → there's an `asyncio.wait_for` at
  twice that budget as a hard guard. If the outer timeout fires,
  the tool returns a JSON envelope with
  `stats.stopped_reason == "hard_timeout"`.

### 5. Live MCP tool discovery via Claude Code / openclaw

Both clients pick up tool-list changes on next session start. No
api restart needed on their side. Open a fresh session and ask:

> Use the `deep_research` tool to write a one-paragraph summary of
> how Python's asyncio event loop scheduler works. Use `max_depth=1`,
> `max_pages=8`, `max_seconds=60`.

The model should fire the tool, wait ~30-60s, then synthesise the
JSON into prose. Verify it cites at least 2 source URLs.

## To do / next steps

### Registry-push failure — fixed in this commit

`docker buildx build --push` defaults to HTTPS when talking to the
registry. The cluster registry at `192.168.0.71:31500` serves plain
HTTP, so the buildx-driven push fails with:

```
http: server gave HTTP response to HTTPS client
```

The `llmmllab-api` deploy avoids this by building each arch with
`--load` (puts the image in the local docker daemon) and then doing
a plain `docker push` — the daemon respects `insecure-registries`
in `/etc/docker/daemon.json`. The workflow was rewritten to follow
that pattern:

```yaml
docker buildx build --builder multibuilder \
  --platform linux/arm64 \
  -t ${REGISTRY}/${IMAGE}:${SHA} \
  --load .

docker push ${REGISTRY}/${IMAGE}:${SHA}
docker tag ${REGISTRY}/${IMAGE}:${SHA} ${REGISTRY}/${IMAGE}:latest
docker push ${REGISTRY}/${IMAGE}:latest
```

Simplifications vs api's workflow:

- **Single arch (arm64) only.** `k8s/deployment.yaml` pins
  `kubernetes.io/arch: arm64`, so the cluster never pulls amd64.
  Building a single arch keeps the workflow short and skips the
  manifest-list step the api needs.
- **No `push_manifest.py`.** Multi-arch manifest creation is needed
  only when you want one image tag to serve multiple architectures;
  with single-arch builds the registry's normal manifest is enough.

If amd64 deployment becomes a requirement later, swap in the api's
two-arch + `push_manifest.py` pattern.

### Promote the runner to org-level (optional cleanup)

Three repo-level runners on the same host (`llmmllab-runner`,
`mcp-server-web`, `openclaw-k8s`) means three systemd services and
three sets of registration tokens. Promoting to a single org-level
runner under `llmmllab` would consolidate to one service, give all
current and future llmmllab repos access automatically, and simplify
runner rotation.

Requires `admin:org` scope on a GitHub PAT (or doing it via the org
Actions settings page in the UI). Out of scope for this batch.

### Documentation polish

- README has tool docs; consider adding an end-to-end recipe section
  ("Drive a research session via Claude Code") with copy-pasteable
  example prompts.
- Document the runner-on-lsnode-3 setup somewhere durable (this file
  or a `k8s/RUNNER.md`).

### Open questions / future work

- **`fetch_with_links` content size.** Currently truncates at
  `MAX_CONTENT_LENGTH` (default 8000 chars). For research workflows
  where the model wants to read entire long-form articles, that's
  tight. Consider exposing a `max_content_chars` parameter so the
  caller can override per-call.
- **`deep_research` content streaming.** The tool currently waits
  until the whole BFS is done before returning. For long runs that's
  30-180s of silence. Splitting into chunked progress events would
  let the calling LLM display intermediate findings — but it requires
  MCP streaming-tool support that fastmcp may or may not currently
  do.
- **Boilerplate regex maintenance.** The `_BOILERPLATE_CLASS_RE` is
  a hand-curated list of ~20 patterns. It misses some sites' custom
  class names. Periodic spot-checking of real-world extractions would
  identify gaps; alternatively swap for `trafilatura` or
  `readability-lxml`, both of which are purpose-built content
  extractors.

## Commit trail

For provenance, the work landed across these commits on `main`:

```
25a468e  fix(http): migrate fetch+search to httpx; explicit MCP registration
78eeab3  adds tests and stuff                              (rebased from other machine)
f1c55e3  ci: actually run the new tests instead of just compile-checking
3502bda  feat(fetch_with_links): LLM-orchestrated research building block
7ad67db  feat(deep-research): iterative web research tool + fetch extraction overhaul
```
