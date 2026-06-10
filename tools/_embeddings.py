"""OpenAI-compatible embeddings client + cosine similarity.

Isolates the only network call in the semantic link-ranking path so the
ranking itself can stay pure/sync and unit-testable.  ``embed_texts`` returns
``None`` on *any* failure (missing config, non-200, timeout, malformed body)
so callers can fall back to lexical ranking without try/except gymnastics.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional, Sequence

import httpx

logger = logging.getLogger("mcp-server-web.embeddings")


async def embed_texts(
    texts: Sequence[str],
    *,
    endpoint: str,
    model: str,
    api_key: str,
    timeout: float,
) -> Optional[list[list[float]]]:
    """Embed ``texts`` via a single OpenAI-compatible ``POST .../embeddings``.

    ``endpoint`` is the base URL (e.g. ``.../v1``); ``/embeddings`` is
    appended.  Returns one vector per input (ordered by the response
    ``index`` field), or ``None`` on any failure.
    """
    if not texts or not endpoint or not model:
        return None

    url = endpoint.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "input": list(texts)}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            logger.debug("embed_texts non-200 (%s) from %s", resp.status_code, url)
            return None
        data = resp.json().get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            return None
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in ordered]
    except (httpx.RequestError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
        logger.debug("embed_texts failed for %s: %s", url, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("embed_texts unexpected error for %s: %s", url, e)
        return None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 on mismatched length or a zero vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
