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


def test_chat_models_and_columns():
    from karox6.providers.catalog import chat_models, columns

    names = [
        "codestral-2508",
        "mistral-embed",
        "mistral-ocr-4",
        "voxtral-mini-tts-3",
        "voxtral-mini-transcribe-3",
        "mistral-moderation-2603",
        "mistral-large-4",
    ]
    assert chat_models(names) == ["codestral-2508", "mistral-large-4"]
    lines = columns([f"m{i}" for i in range(1, 8)], width=40, mark="m2")
    assert lines[0].split() == ["1.", "m1", "4.", "m4", "7.", "m7"]  # numbers go down
    assert "2. m2 *" in lines[1]
    assert columns([]) == []


MISTRAL_LIKE = {
    "data": [
        {
            "id": "deepseek-v4-1-flash-1017",
            "capabilities": {"completion_chat": True, "vision": True},
            "max_context_length": 1048576,
        },
        {"id": "mistral-ocr-latest", "capabilities": {"completion_chat": False, "vision": True}},
        {
            "id": "or/some-model",
            "context_length": 131072,
            "architecture": {"input_modalities": ["text"]},
        },
        {"id": "plain-model"},
    ]
}


def test_what_the_list_says_about_each_model(monkeypatch, tmp_path):
    from karox6.models import model_info, remember
    from karox6.providers.catalog import fetch

    _serve(monkeypatch, lambda request: httpx.Response(200, json=MISTRAL_LIKE))
    models, notes = asyncio.run(fetch("openai-chat", "https://x.test/v1", "k", tmp_path))
    assert models == ["deepseek-v4-1-flash-1017", "or/some-model", "plain-model"]  # no OCR
    assert notes == {"deepseek-v4-1-flash-1017": "1M · pictures", "or/some-model": "128k"}

    here = "https://x.test/v1/"  # the same server, however its URL is written
    info = model_info("deepseek-v4-1-flash-1017", tmp_path, here)
    assert (info.vision, info.context_window, info.source) == (True, 1048576, "provider")
    assert model_info("or/some-model", tmp_path, here).vision is False
    assert model_info("deepseek-v4-1-flash", tmp_path, here).vision is False  # exact name only
    # another server serving the same name: what this list said does not apply there
    other = model_info("deepseek-v4-1-flash-1017", tmp_path, "https://api.deepseek.com/v1")
    assert (other.vision, other.source) == (False, "built-in")
    remember(tmp_path, "deepseek-v4-1-flash-1017", vision=False)  # a refusal beats the list
    assert model_info("deepseek-v4-1-flash-1017", tmp_path, here).vision is False


def test_gemini_and_anthropic_windows(monkeypatch):
    from karox6.providers.catalog import list_entries

    def handler(request):
        if "anthropic" in request.url.host:
            caps = {"image_input": {"supported": True}}
            item = {"id": "claude-x", "max_input_tokens": 200000, "capabilities": caps}
            return httpx.Response(200, json={"data": [item]})
        item = {"name": "models/gemini-9", "inputTokenLimit": 2097152}
        return httpx.Response(200, json={"models": [item]})

    _serve(monkeypatch, handler)
    (claude,) = asyncio.run(list_entries("anthropic", None, "k"))
    assert (claude.context_window, claude.vision, claude.note()) == (
        200000,
        True,
        "200k · pictures",
    )
    (gemini,) = asyncio.run(list_entries("gemini", None, "k"))
    assert (gemini.context_window, gemini.note()) == (2097152, "2M")


def test_a_redirect_to_another_server_is_not_followed(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "x.test" and request.url.path == "/v1/models":
            return httpx.Response(302, headers={"location": "https://x.test/v1/models/"})
        if request.url.host == "x.test":
            return httpx.Response(302, headers={"location": "https://evil.test/steal"})
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    _serve(monkeypatch, handler)
    with pytest.raises(CatalogError, match="another server"):
        asyncio.run(list_models("anthropic", "https://x.test", "secret"))
    assert seen == ["https://x.test/v1/models?limit=1000", "https://x.test/v1/models/"]


@pytest.mark.parametrize(
    "body",
    [
        b'{"data": 5}',
        b'{"data": [{"id": "m", "context_length": 1e999}]}',
        b'{"models": "x"}',
        b"[1, 2]",
    ],
)
def test_odd_lists_do_not_crash(monkeypatch, body):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=body))
    try:
        models = asyncio.run(list_models("openai-chat", "http://x.test/v1", None))
    except CatalogError:
        return
    assert models == ["m"]
