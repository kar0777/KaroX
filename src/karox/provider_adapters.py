"""Dedicated streaming adapters for non-Chat-Completions provider APIs.

The adapters normalize protocol-specific messages, tool calls, usage, and
errors into :mod:`karox.providers` contracts.  They deliberately share only
transport mechanics: protocol event handling stays explicit so an upstream API
change fails as a malformed response instead of being silently reinterpreted.
"""

from __future__ import annotations

from .provider_adapter_base import (  # noqa: F401
    httpx as httpx,
    _openai_effort as _openai_effort,
    _gemini_thinking_level as _gemini_thinking_level,
    stream_error_kind as stream_error_kind,
    _StreamingAdapter as _StreamingAdapter,
    GEMINI_SIGNATURE_PREFIX as GEMINI_SIGNATURE_PREFIX,
    _is_gemini_signature as _is_gemini_signature,
)
from .provider_openai_responses import (  # noqa: F401
    OpenAIResponsesProvider as OpenAIResponsesProvider,
)
from .provider_anthropic import (  # noqa: F401
    _ANTHROPIC_REASONING_BLOCKS as _ANTHROPIC_REASONING_BLOCKS,
    _ANTHROPIC_DEFAULT_MAX_TOKENS as _ANTHROPIC_DEFAULT_MAX_TOKENS,
    AnthropicMessagesProvider as AnthropicMessagesProvider,
)
from .provider_gemini import (  # noqa: F401
    GeminiGenerateContentProvider as GeminiGenerateContentProvider,
)















# Anthropic content blocks that carry the model's own reasoning rather than its
# answer. ``redacted_thinking`` holds an encrypted payload with no readable text;
# both are routed to the reasoning channel and neither becomes assistant content.
#: A Gemini thought signature travels as a reasoning block whose ``data``
#: names the part it belongs to. Gemini 3 rejects a follow-up request whose
#: function-call parts come back without their signatures (HTTP 400), so the
#: opaque value must round-trip. Other transports must never send these: an
#: Anthropic thinking block with a Gemini signature is rejected outright.



# Used only when neither the caller nor the model registry states a ceiling.
# Chosen to be large enough for a substantial code change rather than to match
# any one model's maximum, which KaroX cannot know for an unregistered endpoint.



