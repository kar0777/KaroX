"""The models a provider offers, from its own list (``GET /models`` and the like).

Used by the setup and by ``/models``: the user picks from what their key can reach instead
of typing a name from memory. A server without such a list just yields an error message.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

TIMEOUT_S = 20.0
MAX_PAGES = 20


class CatalogError(Exception):
    pass


async def list_models(
    kind: str, base_url: str | None, api_key: str | None, *, timeout_s: float = TIMEOUT_S
) -> list[str]:
    """Model ids the provider lists for this key, sorted; CatalogError when it cannot say."""
    if kind == "anthropic":
        url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/models"
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        params: dict[str, str] = {"limit": "1000"}
    elif kind == "gemini":
        url = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        url += "/v1beta/models"
        headers = {"x-goog-api-key": api_key} if api_key else {}
        params = {"pageSize": "1000"}
    else:  # the OpenAI-compatible servers (OpenAI, DeepSeek, OpenRouter, vLLM, Ollama...)
        url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        params = {}
    found: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            for _ in range(MAX_PAGES):
                response = await client.get(url, headers=headers, params=params)
                status = response.status_code
                if status in (401, 403):
                    raise CatalogError(f"the server refused the key (HTTP {status})")
                if status >= 400:
                    raise CatalogError(f"the server has no model list (HTTP {status})")
                try:
                    data = response.json()
                except ValueError as exc:
                    raise CatalogError("the model list is not JSON") from exc
                found += _ids(kind, data)
                cursor = _next_page(kind, data)
                if not cursor:
                    break
                params = {**params, **cursor}
    except httpx.HTTPError as exc:
        raise CatalogError(f"cannot reach {url}: {type(exc).__name__}") from exc
    if not found:
        raise CatalogError("the server listed no models")
    return sorted(dict.fromkeys(found), key=str.lower)


def _ids(kind: str, data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    if kind == "gemini":
        out = []
        for m in data.get("models") or []:
            if not isinstance(m, dict) or not isinstance(m.get("name"), str):
                continue
            methods = m.get("supportedGenerationMethods") or ["generateContent"]
            if "generateContent" in methods:
                out.append(m["name"].removeprefix("models/"))
        return out
    items = data.get("data")
    if items is None:
        items = data.get("models")  # Ollama's /api/tags shape, some proxies
    out = []
    for m in items or []:
        if isinstance(m, str):
            out.append(m)
        elif isinstance(m, dict):
            name = m.get("id") or m.get("name") or m.get("model")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def _next_page(kind: str, data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    if kind == "gemini" and isinstance(data.get("nextPageToken"), str) and data["nextPageToken"]:
        return {"pageToken": data["nextPageToken"]}
    if kind == "anthropic" and data.get("has_more") and isinstance(data.get("last_id"), str):
        return {"after_id": data["last_id"]}
    return None


def pick(models: list[str], text: str) -> tuple[str | None, list[str]]:
    """What a typed answer means: (the chosen model, None) or (None, the models it matches).

    A number picks from ``models``; an exact name (any case) picks it; otherwise the names
    containing every word of the answer are the matches.
    """
    text = text.strip()
    if text.isdigit() and 1 <= int(text) <= len(models):
        return models[int(text) - 1], []
    for m in models:
        if m.lower() == text.lower():
            return m, []
    words = text.lower().split()
    return None, [m for m in models if all(w in m.lower() for w in words)]


# Models that do not chat (embeddings, speech, OCR, moderation, images): left out of the
# lists people pick a coding model from.
_NOT_CHAT = re.compile(
    r"embed|moderation|\bocr\b|-ocr|tts|transcri|whisper|realtime|dall-e|image-gen|rerank",
    re.IGNORECASE,
)


def chat_models(models: list[str]) -> list[str]:
    return [m for m in models if not _NOT_CHAT.search(m)]


def columns(models: list[str], width: int = 100, mark: str | None = None) -> list[str]:
    """Numbered names in as many columns as fit ``width`` (numbers go down the columns);
    ``mark`` gets a star."""
    if not models:
        return []
    cells = [f"{i}. {m}{' *' if m == mark else ''}" for i, m in enumerate(models, 1)]
    cell = max(len(c) for c in cells) + 3
    count = max(1, min(4, (width - 2) // cell))
    rows = -(-len(cells) // count)
    lines = []
    for r in range(rows):
        row = [cells[c * rows + r] for c in range(count) if c * rows + r < len(cells)]
        lines.append("  " + "".join(x.ljust(cell) for x in row).rstrip())
    return lines
