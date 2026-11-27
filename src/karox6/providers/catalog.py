"""The models a provider offers, from its own list (``GET /models`` and the like).

Used by the setup and by ``/models``: the user picks from what their key can reach instead
of typing a name from memory. A server without such a list just yields an error message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

TIMEOUT_S = 20.0
MAX_PAGES = 20


class CatalogError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One model of a provider's list, with what the list says about it."""

    id: str
    context_window: int | None = None
    vision: bool | None = None
    chat: bool | None = None  # False: the provider says it does not chat (embeddings...)

    def note(self) -> str:
        """``1M · pictures``: what a picker shows beside the name."""
        parts = []
        if self.context_window:
            from karox6.models import window_text

            parts.append(window_text(self.context_window))
        if self.vision:
            parts.append("pictures")
        return " · ".join(parts)

    def facts(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.context_window:
            out["context_window"] = self.context_window
        if self.vision is not None:
            out["vision"] = self.vision
        return out


async def list_models(
    kind: str, base_url: str | None, api_key: str | None, *, timeout_s: float = TIMEOUT_S
) -> list[str]:
    """Model ids the provider lists for this key, sorted; CatalogError when it cannot say."""
    entries = await list_entries(kind, base_url, api_key, timeout_s=timeout_s)
    return [e.id for e in entries]


async def list_entries(
    kind: str, base_url: str | None, api_key: str | None, *, timeout_s: float = TIMEOUT_S
) -> list[ModelEntry]:
    """The provider's models with what its list says about them, sorted by name."""
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
    found: list[ModelEntry] = []
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
                found += _entries(kind, data)
                cursor = _next_page(kind, data)
                if not cursor:
                    break
                params = {**params, **cursor}
    except httpx.HTTPError as exc:
        raise CatalogError(f"cannot reach {url}: {type(exc).__name__}") from exc
    if not found:
        raise CatalogError("the server listed no models")
    unique = {e.id: e for e in found}
    return sorted(unique.values(), key=lambda e: e.id.lower())


def _entries(kind: str, data: Any) -> list[ModelEntry]:
    if not isinstance(data, dict):
        return []
    if kind == "gemini":
        out = []
        for m in data.get("models") or []:
            if not isinstance(m, dict) or not isinstance(m.get("name"), str):
                continue
            methods = m.get("supportedGenerationMethods") or ["generateContent"]
            if "generateContent" in methods:
                name = m["name"].removeprefix("models/")
                out.append(ModelEntry(name, _int(m.get("inputTokenLimit"))))
        return out
    items = data.get("data")
    if items is None:
        items = data.get("models")  # Ollama's /api/tags shape, some proxies
    out = []
    for m in items or []:
        if isinstance(m, str):
            out.append(ModelEntry(m))
        elif isinstance(m, dict):
            name = m.get("id") or m.get("name") or m.get("model")
            if isinstance(name, str) and name:
                out.append(_described(name, m))
    return out


# Where OpenAI-compatible lists put the window: Mistral, OpenRouter, Groq/Together, vLLM,
# Anthropic.
_WINDOW_KEYS = (
    "max_context_length",
    "context_length",
    "context_window",
    "max_model_len",
    "max_input_tokens",
)


def _described(name: str, m: dict[str, Any]) -> ModelEntry:
    window = None
    for key in _WINDOW_KEYS:
        window = _int(m.get(key))
        if window:
            break
    top = m.get("top_provider")
    if not window and isinstance(top, dict):
        window = _int(top.get("context_length"))
    vision: bool | None = None
    chat: bool | None = None
    caps = m.get("capabilities")
    if isinstance(caps, dict):
        vision = _flag(caps.get("vision", caps.get("image_input")))
        chat = _flag(caps.get("completion_chat", caps.get("chat")))
    arch = m.get("architecture")
    if vision is None and isinstance(arch, dict):
        modalities = arch.get("input_modalities")
        if isinstance(modalities, list) and modalities:
            vision = "image" in modalities
    return ModelEntry(name, window, vision, chat)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return int(value)


def _flag(value: Any) -> bool | None:
    """``true`` / ``{"supported": true}`` -> True; anything else unknown."""
    if isinstance(value, dict):
        value = value.get("supported")
    return value if isinstance(value, bool) else None


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


def chat_entries(entries: Sequence[ModelEntry]) -> list[ModelEntry]:
    """The models one can chat with: what the provider marks as not chatting and what the
    name says is an embedding/speech/OCR model are left out (all, when that leaves none)."""
    kept = [e for e in entries if e.chat is not False and not _NOT_CHAT.search(e.id)]
    return kept or list(entries)


async def fetch(
    kind: str,
    base_url: str | None,
    api_key: str | None,
    home: Path | None,
    lister: Any = None,
) -> tuple[list[str], dict[str, str]]:
    """The chat models to pick from and a note per model (``1M · pictures``); what the
    list says about each model is saved for the session (karox6.models, source
    "provider"). ``lister`` replaces the HTTP call (tests): it may return names only."""
    from karox6.models import remember_listed

    raw = await (lister or list_entries)(kind, base_url, api_key)
    entries = [e if isinstance(e, ModelEntry) else ModelEntry(str(e)) for e in raw]
    remember_listed(home, {e.id: e.facts() for e in entries})
    chat = chat_entries(entries)
    return [e.id for e in chat], {e.id: e.note() for e in chat if e.note()}


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
