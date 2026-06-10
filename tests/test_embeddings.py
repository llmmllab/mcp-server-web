"""Tests for tools/_embeddings.py — OpenAI-compatible client + cosine."""

import httpx
import pytest
import respx

from tools._embeddings import cosine, embed_texts


def test_cosine_identical():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_cosine_orthogonal():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_zero_norm():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_mismatched_length():
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_success():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={"data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]},
        )
    )
    vecs = await embed_texts(
        ["a", "b"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_orders_by_index():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={"data": [
                {"index": 1, "embedding": [9.0]},
                {"index": 0, "embedding": [1.0]},
            ]},
        )
    )
    vecs = await embed_texts(
        ["a", "b"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs == [[1.0], [9.0]]


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_non_200_returns_none():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(500)
    )
    vecs = await embed_texts(
        ["a"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs is None


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_count_mismatch_returns_none():
    respx.post("http://emb/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0]}]}
        )
    )
    vecs = await embed_texts(
        ["a", "b"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert vecs is None


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_sends_auth_header_when_key():
    captured = {}

    def _capture(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0]}]}
        )

    respx.post("http://emb/v1/embeddings").mock(side_effect=_capture)
    await embed_texts(
        ["a"], endpoint="http://emb/v1", model="m", api_key="secret", timeout=5
    )
    assert captured["auth"] == "Bearer secret"


@respx.mock
@pytest.mark.asyncio
async def test_embed_texts_no_auth_header_without_key():
    captured = {}

    def _capture(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0]}]}
        )

    respx.post("http://emb/v1/embeddings").mock(side_effect=_capture)
    await embed_texts(
        ["a"], endpoint="http://emb/v1", model="m", api_key="", timeout=5
    )
    assert captured["auth"] is None


@pytest.mark.asyncio
async def test_embed_texts_guards_empty_inputs():
    assert await embed_texts([], endpoint="http://emb/v1", model="m", api_key="", timeout=5) is None
    assert await embed_texts(["a"], endpoint="", model="m", api_key="", timeout=5) is None
    assert await embed_texts(["a"], endpoint="http://emb/v1", model="", api_key="", timeout=5) is None
