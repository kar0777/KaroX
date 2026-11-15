"""Model providers. ``make_provider`` builds one from a kind name (adapters import lazily)."""

from __future__ import annotations

from typing import Any

from karox6.providers.base import Provider

KINDS = ("openai-chat", "openai-responses", "anthropic", "gemini")


def make_provider(
    kind: str,
    *,
    base_url: str | None,
    api_key: str | None,
    timeout_s: float = 900.0,
    stream: bool = True,
    headers: dict[str, str] | None = None,
) -> Provider:
    extra: dict[str, Any] = {"headers": dict(headers or {})}
    if kind == "openai-chat":
        from karox6.providers.openai_chat import OpenAIChatProvider

        return OpenAIChatProvider(
            base_url=base_url or "https://api.openai.com/v1",
            api_key=api_key,
            timeout_s=timeout_s,
            stream=stream,
            **extra,
        )
    if kind == "anthropic":
        from karox6.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            base_url or "https://api.anthropic.com", api_key, timeout_s, stream, **extra
        )
    if kind == "gemini":
        from karox6.providers.gemini import GeminiProvider

        return GeminiProvider(
            base_url or "https://generativelanguage.googleapis.com",
            api_key,
            timeout_s,
            stream,
            **extra,
        )
    if kind == "openai-responses":
        from karox6.providers.openai_responses import OpenAIResponsesProvider

        return OpenAIResponsesProvider(
            base_url or "https://api.openai.com/v1", api_key, timeout_s, stream, **extra
        )
    raise ValueError(f"provider kind {kind!r} is not available yet (known: {', '.join(KINDS)})")
