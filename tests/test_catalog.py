"""The model list a provider offers (GET /models and the like)."""

import asyncio

import httpx
import pytest

from karox6.providers import catalog
from karox6.providers.catalog import CatalogError, list_models, pick


def _serve(monkeypatch, handler):
    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(catalog.httpx, "AsyncClient", client)


def test_openai_compatible_list(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "b-model"}, {"id": "A-model"}]})

    _serve(monkeypatch, handler)
    models = asyncio.run(list_models("openai-chat", "https://x.test/v1/", "sk-1"))
    assert models == ["A-model", "b-model"]
    assert seen == {"url": "https://x.test/v1/models", "auth": "Bearer sk-1"}


def test_anthropic_pages_and_gemini_names(monkeypatch):
    def handler(request):
        if "anthropic" in request.url.host:
            assert request.headers["x-api-key"] == "k"
            if request.url.params.get("after_id") == "claude-b":
                return httpx.Response(200, json={"data": [{"id": "claude-c"}]})
            data = {"data": [{"id": "claude-a"}, {"id": "claude-b"}], "has_more": True}
            return httpx.Response(200, json={**data, "last_id": "claude-b"})
        assert request.headers["x-goog-api-key"] == "k"
        models = [
            {"name": "models/gemini-3-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding-1", "supportedGenerationMethods": ["embedContent"]},
        ]
        return httpx.Response(200, json={"models": models})

    _serve(monkeypatch, handler)
    assert asyncio.run(list_models("anthropic", None, "k")) == ["claude-a", "claude-b", "claude-c"]
    assert asyncio.run(list_models("gemini", None, "k")) == ["gemini-3-pro"]


@pytest.mark.parametrize(("status", "text"), [(401, "refused the key"), (404, "has no model list")])
def test_errors_say_what_went_wrong(monkeypatch, status, text):
    _serve(monkeypatch, lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(CatalogError, match=text):
        asyncio.run(list_models("openai-chat", "http://x.test/v1", None))


def test_pick():
    models = ["deepseek-chat", "deepseek-reasoner", "glm-5.3-flash"]
    assert pick(models, "2") == ("deepseek-reasoner", [])
    assert pick(models, "GLM-5.3-FLASH") == ("glm-5.3-flash", [])
    assert pick(models, "deepseek") == (None, ["deepseek-chat", "deepseek-reasoner"])
    assert pick(models, "seek chat") == (None, ["deepseek-chat"])
    assert pick(models, "9") == (None, [])
