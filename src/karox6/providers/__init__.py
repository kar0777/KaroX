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
    raise ValueError(f"provider kind {kind!r} is not available yet (known: {', '.join(KINDS)})")
