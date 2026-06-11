---
name: mcp-server-web-test-and-deploy
description: mcp-server-web push-to-main deploy timing + the Playwright/respx test-determinism gotcha
metadata:
  type: project
---

`mcp-server-web` (the `web__*` MCP tools the researcher agent uses; deployed in the `llmmllab` ns).

**Deploy:** push to `main` triggers two GitHub Actions — `CI` (fast, ~20s) and `Deploy MCP Server (web)` (self-hosted, **~38 min** arm64 build via QEMU emulation — though a warm build cache can cut it to ~2 min — then `kubectl rollout restart deployment/mcp-server-web -n llmmllab`). Unlike [[deploy-cuts-active-sessions]] (llmmllab-api), rolling this pod does **not** cut the user's Claude Code session.

**CI gate reality:** the real gate is `pytest` + `py_compile`. The `ruff check`/`ruff format --check` steps use `|| echo warning`, so lint is **advisory and never fails CI**. The repo is not ruff-format-clean (pre-existing).

**Test-determinism gotcha (cost me a debugging detour):** `respx` mocks `httpx` but NOT Playwright. `fetch_page`'s SPA fallback launches real Chromium, so on a dev box with Chromium installed, *short* mocked HTML trips SPA detection (`SPA_TEXT_THRESHOLD=50`) and fetches the **live internet**, failing respx-mocked tests with real page content. CI passes because it has no Chromium binary (launch fails → `None` → uses mocked HTML). `tests/conftest.py` now sets `PLAYWRIGHT_BROWSERS_PATH=/nonexistent-…` at import so launch fails fast → deterministic + CI-equivalent. If that ever regresses, run tests with that env var or expect false failures locally.

**Why:** the local `.venv` has Chromium + network; CI does not — so "passes in CI, fails locally" on fetch tests is this, not a code bug.
**How to apply:** keep the conftest Playwright disable; when adding fetch tests, use content >50 chars to avoid SPA classification, or rely on the conftest setting.

**Embeddings wiring (2026-06-10):** `fetch_with_links` semantic link ranking is live. `llmmllab-api` **already serves** OpenAI-compatible `POST /v1/embeddings` (`routers/openai/embeddings.py`) → proxies to the runner's `nomic-embed-text-v2-moe` (llama.cpp `--embedding --pooling mean`, **768-dim**). `mcp-server-web`'s deployment sets `EMBEDDING_ENDPOINT=http://llmmllab.llmmllab.svc.cluster.local:9999/v1`, `EMBEDDING_MODEL=nomic-embed-text-v2-moe`, `EMBEDDING_API_KEY` from `secretKeyRef openclaw-secrets/LLMMLLAB_API_KEY` (optional:true). **Auth:** llmmllab-api `/v1/*` accepts a raw API key as `Authorization: Bearer <key>` (JWT parse fails → API-key DB-lookup fallback) or `X-API-Key`; valid keys are SHA-256 rows in the `api_keys` Postgres table — the cluster `LLMMLLAB_API_KEY` OpenClaw uses is one. No api/runner code change was needed — just config. Verified end-to-end: HTTP 200, `link_ranking:"embedding"`.

**Wikipedia extraction — FIXED 2026-06-10 (commit 886d1dc):** `_strip_boilerplate` used to decompose the entire `<html>` root because Wikipedia's Vector skin puts feature-flag classes (`vector-feature-language-in-header-enabled`) on `<html>`, and the boilerplate word-regex matched "header" inside that compound token → 0 chars extracted from every Wikipedia page (misclassified SPA). Fix: guard `<html>`/`<body>` in the class-regex pass — document roots are never boilerplate, only their descendants. Verified: /wiki/Vector_database 0→15181 chars, /wiki/PostgreSQL→71081. Lesson: the boilerplate word-regex (`\bheader\b` etc.) over-matches compound CSS tokens; if other sites lose content, suspect the same substring-in-feature-class trap.
