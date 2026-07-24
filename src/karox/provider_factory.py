"""Construct concrete provider adapters from validated registry records."""

from __future__ import annotations

from typing import Callable, Dict

from .credentials import CredentialStore
from .provider_adapters import (
    AnthropicMessagesProvider,
    GeminiGenerateContentProvider,
    OpenAIResponsesProvider,
)
from .providers import OpenAIChatCompletionsProvider, Provider
from .registry import ProviderRecord


ProviderConstructor = Callable[..., Provider]


class ProviderFactory:
    """Resolve an opaque credential reference only when a request is sent."""

    _ADAPTERS: Dict[str, ProviderConstructor] = {
        "openai_compatible_chat": OpenAIChatCompletionsProvider,
        "openai_responses": OpenAIResponsesProvider,
        "anthropic_messages": AnthropicMessagesProvider,
        "gemini_generate_content": GeminiGenerateContentProvider,
    }

    def __init__(self, credentials: CredentialStore | None = None) -> None:
        self.credentials = credentials or CredentialStore()

    def create(self, record: ProviderRecord) -> Provider:
        try:
            constructor = self._ADAPTERS[record.adapter_kind]
        except KeyError as exc:  # defensive: ProviderRecord already validates this
            raise ValueError(
                f"unsupported provider adapter: {record.adapter_kind!r}"
            ) from exc
        credential: Callable[[], str] | None = None
        if record.credential_ref is not None:
            credential = self.credentials.accessor(record.credential_ref)
        return constructor(
            record.base_url,
            credential=credential,
            headers=record.headers,
            query=record.query,
            timeout_seconds=record.timeout_seconds,
            max_transport_retries=record.max_transport_retries,
        )
