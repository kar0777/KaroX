"""Provider transport contracts and the OpenAI-compatible adapter.

This module intentionally has no dependency on :mod:`karox.core`.  Providers
normalize model I/O; they never execute a local action or decide whether an
agent task succeeded.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
)
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from .security import contains_credential, redact


_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The reasoning-depth vocabulary KaroX exposes, ordered cheapest to most
# thorough. Each adapter maps these onto whatever its provider calls the same
# idea, and narrows the top of the range where a provider offers fewer levels.
REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Chat Completions renamed the output cap for its reasoning families, and those
# models reject the old spelling with a 400 rather than ignoring it. The
# provider's error body is deliberately never read -- it can echo the request,
# credentials included -- so the spelling is chosen from the model family rather
# than from the rejection text. A gateway that renames the model past this check
# is covered by routing falling back to another route, not by a wider guess.
_REASONING_FAMILY = re.compile(r"^(?:o[1-9]\d*|gpt-5)(?:[-._]|$)")


def output_token_field(model: str) -> str:
    """Which field this model wants its output ceiling in."""

    name = model.rsplit("/", 1)[-1].strip().lower()
    return "max_completion_tokens" if _REASONING_FAMILY.match(name) else "max_tokens"


# The two KaroX levels above "high". OpenAI's own Chat Completions scale stops
# at "high", so OpenAI-hosted endpoints get those levels clamped there. Other
# OpenAI-compatible gateways publish their own scale -- W&B and DeepSeek accept
# "xhigh" and "max" -- so the level the user chose reaches them unchanged; a
# gateway that still rejects it gets one retry at "high" before any optional
# tuning field is dropped.
_ABOVE_HIGH_EFFORTS = frozenset({"xhigh", "max"})


def _effort_scale_stops_at_high(endpoint: str) -> bool:
    host = (urlsplit(endpoint).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".openai.azure.com")


#: Optional tuning fields an OpenAI-compatible gateway may reject. A retry
#: without them keeps the required shape -- model, messages, tools,
#: tool_choice and the output cap -- so it is the same request, not a weaker
#: one. W&B DeepSeek is the case that made this necessary: it rejects
#: ``reasoning_effort=medium`` while accepting low/high/xhigh/max/none.
#: Optional tuning fields an OpenAI-compatible gateway may reject. A retry
#: without them keeps the required shape -- model, messages, tools,
#: tool_choice and the output cap -- so it is the same request, not a weaker
#: one. W&B DeepSeek is the case that made this necessary: it rejects
#: ``reasoning_effort=medium`` while accepting low/high/xhigh/max/none. Older
#: Azure api-versions and several proxies reject ``stream_options`` instead.
_OPTIONAL_TUNING_KEYS = (
    "reasoning_effort",
    "reasoning",
    "temperature",
    "prompt_cache_key",
    "parallel_tool_calls",
    "stream_options",
)


def _has_optional_tuning(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in _OPTIONAL_TUNING_KEYS)


def _without_optional_tuning(payload: Mapping[str, Any]) -> Dict[str, Any]:
    reduced = {
        key: value for key, value in payload.items() if key not in _OPTIONAL_TUNING_KEYS
    }
    # `tool_choice: auto` is the default; removing it changes nothing, while a
    # `required` choice is part of the request's meaning and stays.
    if reduced.get("tool_choice") == "auto":
        reduced.pop("tool_choice", None)
    return reduced


def _usage_count(value: object) -> int:
    """One usage count, accepting whole floats but rejecting everything else.

    Usage is metadata. A gateway that spells an absent count as ``null`` must
    not invalidate an answer the user has already received, and some gateways
    send whole numbers as floats. Booleans, negatives, fractions and strings
    stay refused: counting them would be worse than saying nothing.
    """

    if isinstance(value, bool):
        raise ValueError("known usage field must be a non-negative integer")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise ValueError("known usage field must be a non-negative integer")
    return value


def _join_identity_fragments(parts: list[str]) -> str:
    """Join streamed id/name fragments, dropping a full repeated identity.

    Some OpenAI-compatible gateways repeat the complete id and name on every
    delta. Plain concatenation produced ``call_1call_1`` and
    ``read_fileread_file``: valid-looking strings that address a tool call
    which does not exist. Exact repeats are ignored; genuine splits still
    concatenate.
    """

    text = ""
    for part in parts:
        if not part:
            continue
        if text and part == text:
            continue
        text += part
    return text


class ProviderErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    MODEL_UNAVAILABLE = "model_unavailable"
    TRANSPORT = "transport"
    PROVIDER_INTERNAL = "provider_internal"
    CANCELLED = "cancelled"
    MALFORMED_RESPONSE = "malformed_response"
    BUDGET_EXCEEDED = "budget_exceeded"


class ProviderError(RuntimeError):
    """A secret-safe, classified provider failure."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        message: str,
        *,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        route_attempts: tuple[Dict[str, Any], ...] = (),
        transport_attempts: int = 1,
    ) -> None:
        if (
            isinstance(transport_attempts, bool)
            or not isinstance(transport_attempts, int)
            or transport_attempts < 1
        ):
            raise ValueError("transport_attempts must be a positive integer")
        safe_message = str(redact(message))[:4000]
        super().__init__(f"{kind.value}: {safe_message}")
        self.kind = kind
        self.safe_message = safe_message
        self.status_code = status_code
        self.retry_after = retry_after
        self.route_attempts = tuple(dict(redact(item)) for item in route_attempts)
        self.transport_attempts = transport_attempts


@dataclass(frozen=True)
class ProviderTool:
    name: str
    description: str
    input_schema: Dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError(
                "provider tool name must match ^[A-Za-z0-9_-]{1,64}$"
            )
        if not isinstance(self.input_schema, dict):
            raise ValueError("provider tool input schema must be an object")


@dataclass(frozen=True)
class ReasoningBlock:
    """One block of a model's own deliberation, kept whole so it can be returned.

    Anthropic signs each thinking block and verifies the signature when the block
    comes back with the tool result it preceded, rejecting anything edited. That
    makes this the one payload KaroX must reproduce byte for byte, which is why
    the text, the opaque form and the signature travel together rather than being
    flattened into the reasoning string shown on screen.
    """

    kind: str
    text: str = ""
    # ``redacted_thinking`` carries an encrypted payload with no readable text.
    data: str = ""
    signature: str = ""
    # False when the stored text is no longer what the model produced, so
    # returning it would be rejected. Kept rather than dropped so the record
    # still shows the model thought here.
    replayable: bool = True

    def __post_init__(self) -> None:
        if self.kind not in {"thinking", "redacted_thinking"}:
            raise ValueError("reasoning block kind is not one this transport models")


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    raw_arguments: str

    def __post_init__(self) -> None:
        if not self.call_id or len(self.call_id) > 500:
            raise ValueError("tool call ID must contain 1-500 characters")
        if not self.name or len(self.name) > 200:
            raise ValueError("tool call name must contain 1-200 characters")
        if not isinstance(self.raw_arguments, str):
            raise ValueError("tool call arguments must be JSON text")


@dataclass(frozen=True)
class ImageAttachment:
    mime: str
    data: bytes

    def __post_init__(self) -> None:
        if self.mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("image attachment must be PNG, JPEG, or WebP")
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("image attachment data must be non-empty bytes")
        if len(self.data) > 10 * 1024 * 1024:
            raise ValueError("image attachment exceeds 10 MiB")

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: Optional[str] = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: Optional[str] = None
    # The deliberation that preceded this turn's content, in the order the model
    # produced it. A transport that signs its thinking needs it back.
    reasoning_blocks: tuple[ReasoningBlock, ...] = ()
    # Request-only multimodal content. Kept last so existing positional
    # ModelMessage(role, content, tool_calls, ...) call sites retain their exact
    # meaning; new code passes images by keyword.
    images: tuple[ImageAttachment, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported model message role: {self.role!r}")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("model message content must be text or null")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require a tool call ID")
        if self.images and self.role != "user":
            raise ValueError("only user messages may contain image attachments")
        if len(self.images) > 8:
            raise ValueError("a model message may contain at most 8 image attachments")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages may contain tool calls")
        if self.role != "assistant" and self.reasoning_blocks:
            raise ValueError("only assistant messages may carry reasoning blocks")


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ProviderTool, ...] = ()
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    deadline_seconds: float = 120.0
    # A stable identifier for the conversation this request continues. An agent
    # loop resends its whole transcript on every step, so the same prefix is
    # paid for again and again at full price; providers will serve that prefix
    # from cache at a fraction of the cost, but only when the caller asks. It is
    # opt-in because a one-shot request would pay the cache-write premium and
    # never read it back.
    cache_key: Optional[str] = None
    # How hard the model should think before answering. One dial, spelled the
    # same for every provider, because the caller is choosing an intent -- cheap
    # and fast, or slow and careful -- not a vendor's parameter name.
    reasoning_effort: Optional[str] = None
    # Whether the provider may answer with prose only or must select a tool.
    # Recovery turns use ``required`` so an output budget cannot be spent on
    # another long explanation before the action the runtime already knows is due.
    tool_choice: str = "auto"
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.model or len(self.model) > 500:
            raise ValueError("model ID must contain 1-500 characters")
        if not self.messages:
            raise ValueError("model request requires at least one message")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(float(self.deadline_seconds))
            or not 0.05 <= float(self.deadline_seconds) <= 3600
        ):
            raise ValueError("model request deadline must be between 0.05 and 3600 seconds")
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(float(self.temperature))
        ):
            raise ValueError("temperature must be finite")
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("max output tokens must be a positive integer")
        if self.cache_key is not None and (
            not isinstance(self.cache_key, str)
            or not self.cache_key.strip()
            or len(self.cache_key) > 200
        ):
            raise ValueError("cache key must be 1-200 non-blank characters")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in REASONING_EFFORTS
        ):
            raise ValueError(
                "reasoning effort must be one of " + ", ".join(sorted(REASONING_EFFORTS))
            )
        if self.tool_choice not in {"auto", "required"}:
            raise ValueError("tool choice must be 'auto' or 'required'")


@dataclass(frozen=True)
class ModelResponse:
    content: Optional[str]
    tool_calls: tuple[ToolCall, ...]
    finish_reason: Optional[str]
    usage: Dict[str, int]
    response_id: Optional[str] = None
    transport_attempts: int = 1
    transport_metrics: Dict[str, float] = field(default_factory=dict)
    route_attempts: tuple[Dict[str, Any], ...] = ()
    selected_provider: Optional[str] = None
    selected_model: Optional[str] = None
    cost: Optional[float] = None
    #: Why cost is absent: "usage_missing" (the gateway did not report prompt
    #: or completion counters) or "no_pricing" (this model has no price record).
    #: None means the cost below is a measured value, not that it was zero.
    cost_unknown_reason: Optional[str] = None
    # The same response priced with every prompt token at the normal input rate.
    # This lets the UI report the measured cache effect without inventing a
    # second price table or guessing what the provider would have charged.
    uncached_cost: Optional[float] = None
    cache_savings: Optional[float] = None
    currency: Optional[str] = None
    pricing_version: Optional[str] = None
    cumulative_usage: Dict[str, int] = field(default_factory=dict)
    cumulative_cost: Optional[float] = None
    budget_exceeded: bool = False
    budget_reason: Optional[str] = None
    # Whatever raw reasoning the provider chose to expose, kept apart from
    # ``content`` so it is never mistaken for the model's answer.
    reasoning: Optional[str] = None
    # A provider-labeled high-level summary is a different, explicitly public
    # channel. It may be shown as progress; raw ``reasoning`` above may not.
    reasoning_summary: Optional[str] = None
    # The same reasoning still in blocks, with the signatures that authenticate
    # them. ``reasoning`` is for showing a human; this is for handing back.
    reasoning_blocks: tuple[ReasoningBlock, ...] = ()


class ModelEventKind(str, Enum):
    TEXT_DELTA = "text_delta"
    # A model's own reasoning, kept on a separate channel from the answer.
    # Merging it into TEXT_DELTA would put private deliberation into the
    # assistant content that KaroX persists and re-sends as the answer.
    REASONING_DELTA = "reasoning_delta"
    # A provider explicitly labeled this as a high-level summary rather than
    # private reasoning text. This is the only reasoning channel UI may render.
    REASONING_SUMMARY_DELTA = "reasoning_summary_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    USAGE = "usage"
    COMPLETION = "completion"


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    call_id_fragment: str = ""
    name_fragment: str = ""
    arguments_fragment: str = ""


@dataclass(frozen=True)
class ModelEvent:
    kind: ModelEventKind
    text_delta: Optional[str] = None
    reasoning_delta: Optional[str] = None
    reasoning_summary_delta: Optional[str] = None
    # Emitted once, when a reasoning block closes and its text and signature are
    # both complete. The deltas above stream the same words to a screen; this is
    # the whole block, kept so it can be returned to the provider intact.
    reasoning_block: Optional[ReasoningBlock] = None
    tool_call_delta: Optional[ToolCallDelta] = None
    usage: Dict[str, int] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    response_id: Optional[str] = None
    transport_attempts: int = 1
    transport_metrics: Dict[str, float] = field(default_factory=dict)
    # Set only on ``COMPLETION`` by a layer that knows more about the call than
    # the deltas can carry — which route served it, what it cost, whether a
    # budget is now spent. A consumer that streams must not have to re-derive
    # that from the transport, so the authoritative response travels with the
    # terminal event instead of being available only from ``complete()``.
    response: Optional[ModelResponse] = None


def accumulate_response(
    events: Iterable[ModelEvent],
    *,
    observer: Optional[Callable[[ModelEvent], None]] = None,
) -> ModelResponse:
    """Fold a normalized event stream into one response.

    This is the single definition of "what a stream adds up to", shared by
    every transport so a streaming consumer and a blocking one can never
    disagree about the same bytes. ``observer`` sees each event as it arrives,
    which is what makes live output and a final response the same pass.
    """

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_summary_parts: list[str] = []
    reasoning_blocks: list[ReasoningBlock] = []
    saw_content = False
    call_parts: Dict[int, Dict[str, list[str]]] = {}
    usage: Dict[str, int] = {}
    finish_reason: Optional[str] = None
    response_id: Optional[str] = None
    transport_attempts = 1
    transport_metrics: Dict[str, float] = {}
    completed = False
    decorated: Optional[ModelResponse] = None

    for event in events:
        if observer is not None:
            observer(event)
        transport_attempts = event.transport_attempts
        if event.transport_metrics:
            transport_metrics.update(event.transport_metrics)
        response_id = event.response_id or response_id
        if event.kind == ModelEventKind.TEXT_DELTA:
            saw_content = True
            content_parts.append(event.text_delta or "")
        elif event.kind == ModelEventKind.REASONING_DELTA:
            reasoning_parts.append(event.reasoning_delta or "")
            if event.reasoning_block is not None:
                reasoning_blocks.append(event.reasoning_block)
        elif event.kind == ModelEventKind.REASONING_SUMMARY_DELTA:
            reasoning_summary_parts.append(event.reasoning_summary_delta or "")
        elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
            delta = event.tool_call_delta
            if delta is None:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "tool-call event omitted its delta",
                )
            parts = call_parts.setdefault(
                delta.index,
                {"call_id": [], "name": [], "arguments": []},
            )
            parts["call_id"].append(delta.call_id_fragment)
            parts["name"].append(delta.name_fragment)
            parts["arguments"].append(delta.arguments_fragment)
        elif event.kind == ModelEventKind.USAGE:
            usage.update(event.usage)
        elif event.kind == ModelEventKind.COMPLETION:
            completed = True
            finish_reason = event.finish_reason
            if event.response is not None:
                decorated = event.response

    if not completed:
        raise ProviderError(
            ProviderErrorKind.TRANSPORT,
            "provider stream ended before completion",
        )
    if decorated is not None:
        return decorated
    try:
        tool_calls = tuple(
            ToolCall(
                call_id=_join_identity_fragments(parts["call_id"]),
                name=_join_identity_fragments(parts["name"]),
                raw_arguments="".join(parts["arguments"]),
            )
            for _, parts in sorted(call_parts.items())
        )
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            ProviderErrorKind.MALFORMED_RESPONSE,
            f"invalid streamed tool call: {exc}",
        ) from exc
    reasoning = "".join(reasoning_parts)
    reasoning_summary = "".join(reasoning_summary_parts)
    return ModelResponse(
        content="".join(content_parts) if saw_content else None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        response_id=response_id,
        transport_attempts=transport_attempts,
        transport_metrics=dict(transport_metrics),
        reasoning=reasoning or None,
        reasoning_summary=reasoning_summary or None,
        reasoning_blocks=tuple(reasoning_blocks),
    )


class Provider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


def close_provider_quietly(provider: object) -> None:
    """Release an optional provider-owned transport without masking real work."""

    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # Cleanup must never replace a provider result/error with a socket-close
        # failure. Persistent transports are an optimization, not task semantics.
        return


CredentialAccessor = Callable[[], str]


_RESERVED_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key"}
)
_RESERVED_QUERY_NAMES = frozenset(
    {"access_token", "api_key", "apikey", "key", "token"}
)


class OpenAIChatCompletionsProvider:
    """Synchronous streaming generic Chat Completions transport.

    Only failures before a usable HTTP response are retried.  A provider HTTP
    error or malformed response is surfaced once and is never reinterpreted as
    proof that a local mutation should be repeated.
    """

    MAX_SSE_LINE_CHARS = 1_048_576
    # Reasoning-heavy gateways can legitimately emit one tiny SSE frame per
    # reasoning token/fragment. Ox Alpha on ultra exceeded the old 10k ceiling
    # before producing its first tool call. Keep a generous event-count bound,
    # while the independent 16 MiB byte ceiling remains the hard resource cap.
    MAX_SSE_EVENTS = 100_000
    MAX_SSE_TOTAL_CHARS = 16_777_216

    def __init__(
        self,
        base_url: str,
        *,
        credential: Optional[CredentialAccessor] = None,
        headers: Optional[Mapping[str, str]] = None,
        query: Optional[Mapping[str, str]] = None,
        timeout_seconds: float = 60.0,
        max_transport_retries: int = 2,
        retry_backoff_seconds: float = 0.1,
    ) -> None:
        self.endpoint = self._endpoint(base_url, query or {})
        if credential is not None and not self._credential_transport_is_secure(
            self.endpoint
        ):
            raise ValueError(
                "provider credentials require HTTPS or a loopback HTTP endpoint"
            )
        self._credential = credential
        self._headers = self._validate_headers(headers or {})
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.05 <= float(timeout_seconds) <= 3600
        ):
            raise ValueError("provider timeout must be between 0.05 and 3600 seconds")
        if (
            isinstance(max_transport_retries, bool)
            or not isinstance(max_transport_retries, int)
            or not 0 <= max_transport_retries <= 10
        ):
            raise ValueError("transport retries must be between 0 and 10")
        if (
            isinstance(retry_backoff_seconds, bool)
            or not isinstance(retry_backoff_seconds, (int, float))
            or not math.isfinite(float(retry_backoff_seconds))
            or retry_backoff_seconds < 0
        ):
            raise ValueError("retry backoff must be finite and non-negative")
        self.timeout_seconds = float(timeout_seconds)
        self.max_transport_retries = max_transport_retries
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._client_lock = threading.Lock()
        self._client: Optional[httpx.Client] = None
        # Per-model: a model that accepted a reduced request starts reduced
        # next time; one whose reduced request also failed is never retried
        # again. Keying on the provider alone would disable a working dial for
        # every other model on the same endpoint.
        self._omit_optional_tuning_models: set[str] = set()
        self._reduced_failed_models: set[str] = set()
        self._effort_clamped_models: set[str] = set()

    def _http_client(self) -> httpx.Client:
        client = self._client
        if client is not None and not bool(getattr(client, "is_closed", False)):
            return client
        with self._client_lock:
            client = self._client
            if client is None or bool(getattr(client, "is_closed", False)):
                client = httpx.Client(
                    follow_redirects=False,
                    limits=httpx.Limits(
                        max_connections=100,
                        max_keepalive_connections=20,
                        keepalive_expiry=120.0,
                    ),
                )
                self._client = client
            return client

    def close(self) -> None:
        with self._client_lock:
            client = self._client
            self._client = None
        closer = getattr(client, "close", None) if client is not None else None
        if callable(closer):
            closer()

    @property
    def provider_name(self) -> str:
        return "openai_compatible_chat_completions"

    @staticmethod
    def _endpoint(base_url: str, query: Mapping[str, str]) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("provider base URL is required")
        parts = urlsplit(base_url.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "provider base URL must be HTTP(S) without credentials, query, or fragment"
            )
        safe_query: Dict[str, str] = {}
        for raw_name, raw_value in query.items():
            name, value = str(raw_name), str(raw_value)
            if name.lower() in _RESERVED_QUERY_NAMES or contains_credential(value):
                raise ValueError("provider query cannot contain credentials")
            if not name or len(name) > 200 or len(value) > 2000:
                raise ValueError("provider query contains an invalid name or value")
            safe_query[name] = value
        path = parts.path.rstrip("/") + "/chat/completions"
        return urlunsplit((parts.scheme, parts.netloc, path, urlencode(safe_query), ""))

    @staticmethod
    def _credential_transport_is_secure(endpoint: str) -> bool:
        parts = urlsplit(endpoint)
        if parts.scheme == "https":
            return True
        hostname = (parts.hostname or "").lower()
        if hostname == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _validate_headers(headers: Mapping[str, str]) -> Dict[str, str]:
        safe: Dict[str, str] = {}
        for raw_name, raw_value in headers.items():
            name, value = str(raw_name), str(raw_value)
            if name.lower() in _RESERVED_HEADERS or contains_credential(value):
                raise ValueError("custom provider headers cannot contain credentials")
            if not name or "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                raise ValueError("provider header contains invalid characters")
            safe[name] = value
        return safe

    @staticmethod
    def _message_payload(message: ModelMessage) -> Dict[str, Any]:
        if message.images:
            content: list[Dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": image.data_url, "detail": "auto"},
                }
                for image in message.images
            )
            payload: Dict[str, Any] = {"role": message.role, "content": content}
        else:
            payload = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": item.call_id,
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "arguments": item.raw_arguments,
                    },
                }
                for item in message.tool_calls
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @classmethod
    def _request_payload(cls, request: ModelRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": request.model,
            "messages": [cls._message_payload(item) for item in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description,
                        "parameters": item.input_schema,
                    },
                }
                for item in request.tools
            ]
            payload["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            # Reasoning families (o1/o3.../gpt-5...) reject ``temperature``
            # with a 400, the same way they reject the old output-cap field.
            # The family regex already picks their token field; the sampling
            # dial has to respect it too, or one preset temperature kills the
            # whole request on those models.
            if not _REASONING_FAMILY.match(
                request.model.rsplit("/", 1)[-1].strip().lower()
            ):
                payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload[output_token_field(request.model)] = request.max_output_tokens
        if request.cache_key is not None:
            # Caching is automatic on this wire; the key only routes requests
            # that share a prefix to the same cache, which every step of an
            # agent loop does.
            payload["prompt_cache_key"] = request.cache_key
        if request.reasoning_effort is not None:
            # Chat Completions spells the dial flat. `stream` narrows a level
            # above "high" only where the endpoint's scale stops there.
            payload["reasoning_effort"] = request.reasoning_effort
        return payload

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **self._headers,
        }
        if self._credential is not None:
            try:
                secret = self._credential()
            except Exception as exc:
                raise ProviderError(
                    ProviderErrorKind.AUTHENTICATION,
                    f"credential accessor failed: {type(exc).__name__}",
                ) from exc
            if not isinstance(secret, str) or not secret.strip():
                raise ProviderError(
                    ProviderErrorKind.AUTHENTICATION,
                    "credential accessor returned no API key",
                )
            if "\r" in secret or "\n" in secret:
                raise ProviderError(
                    ProviderErrorKind.AUTHENTICATION,
                    "credential contains invalid characters",
                )
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]:
        payload = self._request_payload(request)
        # A level above "high" is clamped only where the scale is known to stop
        # there, or where this model has already rejected it once.
        if payload.get("reasoning_effort") in _ABOVE_HIGH_EFFORTS and (
            _effort_scale_stops_at_high(self.endpoint)
            or request.model in self._effort_clamped_models
        ):
            payload["reasoning_effort"] = "high"
        # OpenRouter can return an explicit high-level reasoning summary without
        # changing the model's reasoning budget. Keep the existing flat
        # reasoning_effort exactly as before and only request summary display;
        # other OpenAI-compatible gateways never see this OpenRouter extension.
        hostname = (urlsplit(self.endpoint).hostname or "").lower()
        if request.reasoning_effort is not None and (
            hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")
        ):
            payload["reasoning"] = {"summary": "auto"}
        if request.model in self._omit_optional_tuning_models:
            payload = _without_optional_tuning(payload)
        tuning_retry = False
        effort_retry = False
        first_invalid: Optional[ProviderError] = None
        headers = self._request_headers()
        request_started = time.monotonic()
        deadline = request_started + float(request.deadline_seconds)
        attempts = 0
        while True:
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT,
                    "provider request deadline expired",
                )
            response_started = False
            event_started = False
            try:
                client = self._http_client()
                with client.stream(
                    "POST",
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=min(self.timeout_seconds, max(remaining, 0.05)),
                ) as response:
                    response_started = True
                    response_headers_ms = round(
                        max(0.0, (time.monotonic() - request_started) * 1000.0),
                        3,
                    )
                    if response.status_code >= 400:
                        raise self._http_error(response)
                    first_event_ms: Optional[float] = None
                    try:
                        for event in self._stream_events(response, attempts, deadline):
                            now = time.monotonic()
                            if first_event_ms is None:
                                first_event_ms = round(
                                    max(0.0, (now - request_started) * 1000.0),
                                    3,
                                )
                            if event.kind is ModelEventKind.COMPLETION:
                                event = replace(
                                    event,
                                    transport_metrics={
                                        "response_headers_ms": response_headers_ms,
                                        "first_event_ms": first_event_ms,
                                        "total_ms": round(
                                            max(
                                                0.0,
                                                (now - request_started) * 1000.0,
                                            ),
                                            3,
                                        ),
                                    },
                                )
                            event_started = True
                            yield event
                    except httpx.TransportError as exc:
                        if event_started:
                            raise ProviderError(
                                ProviderErrorKind.TRANSPORT,
                                f"provider stream interrupted: {type(exc).__name__}",
                                status_code=response.status_code,
                                transport_attempts=attempts,
                            ) from exc
                        raise
                    # The reduced request was accepted: start reduced for this
                    # model next time instead of paying the rejection again.
                    if tuning_retry:
                        self._omit_optional_tuning_models.add(request.model)
                    elif effort_retry:
                        self._effort_clamped_models.add(request.model)
                    return
            except ProviderError as exc:
                if (
                    exc.kind is ProviderErrorKind.INVALID_REQUEST
                    and not effort_retry
                    and not tuning_retry
                    and payload.get("reasoning_effort") in _ABOVE_HIGH_EFFORTS
                ):
                    # This gateway's effort scale may stop at "high". Retry
                    # once there with everything else unchanged before
                    # treating the optional tuning fields as the cause.
                    first_invalid = exc
                    effort_retry = True
                    payload = {**payload, "reasoning_effort": "high"}
                    continue
                if (
                    exc.kind is ProviderErrorKind.INVALID_REQUEST
                    and not tuning_retry
                    and request.model not in self._omit_optional_tuning_models
                    and request.model not in self._reduced_failed_models
                    and _has_optional_tuning(payload)
                ):
                    # A gateway with a different parameter vocabulary rejects
                    # a tuning field this product always sends. Retry once
                    # with the optional fields removed; the error body is never
                    # read, so nothing it echoes can reach a log. The reduced
                    # shape is remembered per model only after it succeeds.
                    if first_invalid is None:
                        first_invalid = exc
                    tuning_retry = True
                    payload = _without_optional_tuning(payload)
                    continue
                if (
                    exc.kind is ProviderErrorKind.INVALID_REQUEST
                    and tuning_retry
                    and first_invalid is not None
                ):
                    # The reduced request failed too, so tuning was not the
                    # cause; report the first error and never pay this retry
                    # for this model again.
                    self._reduced_failed_models.add(request.model)
                    raise first_invalid
                raise
            except httpx.TransportError as exc:
                if response_started and event_started:
                    raise ProviderError(
                        ProviderErrorKind.TRANSPORT,
                        f"provider stream interrupted: {type(exc).__name__}",
                        transport_attempts=attempts,
                    ) from exc
                if attempts >= self.max_transport_retries + 1:
                    raise ProviderError(
                        ProviderErrorKind.TRANSPORT,
                        f"provider transport failed: {type(exc).__name__}",
                        transport_attempts=attempts,
                    ) from exc
                delay = self.retry_backoff_seconds * (2 ** (attempts - 1))
                if delay:
                    if delay >= deadline - time.monotonic():
                        raise ProviderError(
                            ProviderErrorKind.TRANSPORT,
                            "provider request deadline expired during retry",
                            transport_attempts=attempts,
                        ) from exc
                    time.sleep(delay)
                continue

    def complete(self, request: ModelRequest) -> ModelResponse:
        return accumulate_response(self.stream(request))

    @classmethod
    def _stream_events(
        cls,
        response: httpx.Response,
        attempts: int,
        deadline: float,
    ) -> Iterator[ModelEvent]:
        data_lines: list[str] = []
        finish_reason: Optional[str] = None
        response_id: Optional[str] = None
        event_count = 0
        total_chars = 0
        terminal_event: Optional[ModelEvent] = None

        for line in response.iter_lines():
            if time.monotonic() >= deadline:
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT,
                    "provider request deadline expired while reading stream",
                    status_code=response.status_code,
                    transport_attempts=attempts,
                )
            if not isinstance(line, str):
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contained a non-text line",
                    status_code=response.status_code,
                )
            if len(line) > cls.MAX_SSE_LINE_CHARS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE line exceeds the size limit",
                    status_code=response.status_code,
                )
            total_chars += len(line) + 1
            if total_chars > cls.MAX_SSE_TOTAL_CHARS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream exceeds the size limit",
                    status_code=response.status_code,
                )
            if line == "":
                if not data_lines:
                    continue
                event_count += 1
                if event_count > cls.MAX_SSE_EVENTS:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "provider SSE stream contains too many events",
                        status_code=response.status_code,
                    )
                data = "\n".join(data_lines)
                data_lines.clear()
                if terminal_event is not None:
                    # The protocol is complete, but the HTTP response must still
                    # reach EOF before its socket can return to the client pool.
                    continue
                if data.strip() == "[DONE]":
                    terminal_event = ModelEvent(
                        ModelEventKind.COMPLETION,
                        finish_reason=finish_reason,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                    continue
                events, chunk_finish, chunk_id = cls._parse_stream_chunk(
                    data, response.status_code, attempts
                )
                if chunk_finish is not None:
                    finish_reason = chunk_finish
                if chunk_id is not None:
                    if response_id is not None and response_id != chunk_id:
                        raise ProviderError(
                            ProviderErrorKind.MALFORMED_RESPONSE,
                            "provider changed response ID during stream",
                            status_code=response.status_code,
                        )
                    response_id = chunk_id
                yield from events
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value if separator else "")

        if data_lines and terminal_event is None:
            event_count += 1
            if event_count > cls.MAX_SSE_EVENTS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contains too many events",
                    status_code=response.status_code,
                )
            data = "\n".join(data_lines)
            if data.strip() == "[DONE]":
                terminal_event = ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason=finish_reason,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
            else:
                cls._parse_stream_chunk(data, response.status_code, attempts)
        if terminal_event is not None:
            yield terminal_event
            return
        raise ProviderError(
            ProviderErrorKind.TRANSPORT,
            "provider stream ended before [DONE]",
            status_code=response.status_code,
            transport_attempts=attempts,
        )

    @staticmethod
    def _parse_stream_chunk(
        data: str,
        status_code: int,
        attempts: int,
    ) -> tuple[list[ModelEvent], Optional[str], Optional[str]]:
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                "provider SSE event contained invalid JSON",
                status_code=status_code,
            ) from exc

        try:
            if not isinstance(value, dict):
                raise ValueError("event root must be an object")
            response_id = value.get("id")
            if response_id is not None and not isinstance(response_id, str):
                raise ValueError("response ID must be text or null")

            events: list[ModelEvent] = []
            raw_usage = value.get("usage")
            if raw_usage is not None:
                if not isinstance(raw_usage, dict):
                    raise ValueError("usage must be an object")
                usage: Dict[str, int] = {}
                # prompt_tokens counts the whole prompt on this wire, but the
                # cached share of it bills at a tenth of the rate, so a cost
                # figure computed without it is simply wrong. Caching also
                # fails silently, and this count is the only evidence it worked.
                details = raw_usage.get("prompt_tokens_details")
                detail_source = details if isinstance(details, dict) else {}
                for name, source, target in (
                    ("prompt_tokens", raw_usage, "prompt_tokens"),
                    ("completion_tokens", raw_usage, "completion_tokens"),
                    ("total_tokens", raw_usage, "total_tokens"),
                ):
                    raw_count = source.get(name)
                    if raw_count is None:
                        # A gateway may spell an absent count as null. Usage is
                        # metadata; it must not invalidate a complete answer.
                        continue
                    usage[target] = _usage_count(raw_count)
                # Cache accounting has three spellings on this wire. The detail
                # object is OpenAI's; prompt_cache_hit_tokens is DeepSeek's
                # (the live W&B route), and cache_read_input_tokens appears on
                # some proxies. First present wins; counting two would inflate
                # the cached share and understate the cost.
                for name, source in (
                    ("cached_tokens", detail_source),
                    ("prompt_cache_hit_tokens", raw_usage),
                    ("cache_read_input_tokens", raw_usage),
                ):
                    raw_count = source.get(name)
                    if raw_count is None:
                        continue
                    usage["cache_read_tokens"] = _usage_count(raw_count)
                    break
                events.append(
                    ModelEvent(
                        ModelEventKind.USAGE,
                        usage=usage,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                )

            choices = value.get("choices")
            if choices is None and raw_usage is not None:
                # A usage-only terminal chunk may omit choices entirely; the
                # empty-choices path below already treats that as a keepalive.
                choices = []
            if not isinstance(choices, list):
                raise ValueError("choices must be an array")
            if not choices:
                # An empty-choices chunk with no usage is a gateway keepalive
                # frame, not a contract violation: some OpenAI-compatible
                # gateways emit them during long generations. The other
                # adapters tolerate unknown/empty events, so this one must not
                # kill an otherwise healthy stream either.
                return events, None, response_id

            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("choice must be an object")
            # OpenAI-compatible gateways are not perfectly uniform at the
            # terminal/tool-call boundaries. OpenRouter can emit ``delta: null``
            # on a finish-only chunk. That is an empty fragment, not a malformed
            # response. Non-null non-objects remain invalid so bad schemas still
            # fail closed.
            raw_delta = choice.get("delta")
            if raw_delta is None:
                delta: Dict[str, Any] = {}
            elif isinstance(raw_delta, dict):
                delta = raw_delta
            else:
                raise ValueError("choice delta must be an object or null")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("content delta must be text or null")
                events.append(
                    ModelEvent(
                        ModelEventKind.TEXT_DELTA,
                        text_delta=content,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                )

            # Reasoning models on the OpenAI-compatible wire may put raw private
            # deliberation on a separate key. Preserve it on the private reasoning
            # channel for protocol/replay needs, but never merge it into content or
            # public progress; both spellings are used by compatible gateways.
            for name in ("reasoning_content", "reasoning"):
                thought = delta.get(name)
                if isinstance(thought, str) and thought:
                    events.append(
                        ModelEvent(
                            ModelEventKind.REASONING_DELTA,
                            reasoning_delta=thought,
                            response_id=response_id,
                            transport_attempts=attempts,
                        )
                    )
                    break

            # OpenRouter standardizes structured reasoning into reasoning_details.
            # Only the explicitly public ``reasoning.summary`` type may cross into
            # KaroX progress UI. Raw ``reasoning.text`` and encrypted details are
            # deliberately ignored here; the raw reasoning channel above remains
            # private and never becomes assistant/progress content.
            raw_reasoning_details = delta.get("reasoning_details")
            if raw_reasoning_details is not None:
                if not isinstance(raw_reasoning_details, list):
                    raise ValueError("reasoning_details must be an array or null")
                for detail in raw_reasoning_details:
                    if not isinstance(detail, dict):
                        raise ValueError("reasoning detail must be an object")
                    detail_type = detail.get("type")
                    if detail_type != "reasoning.summary":
                        continue
                    summary = detail.get("summary")
                    if summary is not None and not isinstance(summary, str):
                        raise ValueError("reasoning summary must be text or null")
                    if summary:
                        events.append(
                            ModelEvent(
                                ModelEventKind.REASONING_SUMMARY_DELTA,
                                reasoning_summary_delta=summary,
                                response_id=response_id,
                                transport_attempts=attempts,
                            )
                        )

            # Streaming tool calls are fragments. Only the first chunk is
            # required to carry id/name; later chunks commonly carry those fields
            # as null and append only ``arguments``. Treat null optional fields as
            # empty fragments, while preserving strict validation for genuinely
            # wrong types. The accumulated ToolCall still requires a non-empty
            # final id and name, so an actually incomplete stream is rejected at
            # assembly time.
            raw_calls = delta.get("tool_calls")
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list):
                raise ValueError("tool-call deltas must be an array or null")
            for raw in raw_calls:
                if not isinstance(raw, dict):
                    raise ValueError("tool-call delta must be an object")
                index = raw.get("index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or index < 0
                ):
                    raise ValueError("tool-call delta requires a non-negative index")
                raw_type = raw.get("type")
                # Later fragments of one call carry the identity fields as empty
                # strings rather than null on some OpenAI-compatible endpoints
                # (measured on StepFun's Step Plan endpoint: the first chunk has
                # the id and type, the rest send {"id": "", "type": ""} while
                # appending arguments). An empty string is that same empty
                # fragment, not a different kind of tool call.
                if isinstance(raw_type, str) and not raw_type.strip():
                    raw_type = None
                if raw_type is not None and raw_type != "function":
                    raise ValueError("only function tool-call deltas are supported")
                raw_call_id = raw.get("id")
                if raw_call_id is not None and not isinstance(raw_call_id, str):
                    raise ValueError("tool-call ID must be text or null")
                raw_function = raw.get("function")
                if raw_function is None:
                    function: Dict[str, Any] = {}
                elif isinstance(raw_function, dict):
                    function = raw_function
                else:
                    raise ValueError("tool-call function must be an object or null")
                raw_name = function.get("name")
                raw_arguments = function.get("arguments")
                if raw_name is not None and not isinstance(raw_name, str):
                    raise ValueError("tool-call name must be text or null")
                if raw_arguments is not None and not isinstance(raw_arguments, str):
                    raise ValueError("tool-call arguments must be text or null")
                call_id = raw_call_id or ""
                name = raw_name or ""
                arguments = raw_arguments or ""
                events.append(
                    ModelEvent(
                        ModelEventKind.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(
                            index=index,
                            call_id_fragment=call_id,
                            name_fragment=name,
                            arguments_fragment=arguments,
                        ),
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                )

            finish_reason = choice.get("finish_reason")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise ValueError("finish reason must be text or null")
            return events, finish_reason, response_id
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                f"invalid Chat Completions stream event: {exc}",
                status_code=status_code,
            ) from exc

    @staticmethod
    def _retry_after(raw_value: Optional[str]) -> Optional[float]:
        """Return the wait a provider asked for, in seconds.

        RFC 9110 allows ``Retry-After`` to carry either a delay in seconds or an
        HTTP date, and real gateways send both.  Reading only the number threw
        the date form away and left the router guessing its own backoff.
        """
        if not raw_value:
            return None
        try:
            seconds = float(raw_value)
        except ValueError:
            try:
                moment = parsedate_to_datetime(raw_value)
            except (TypeError, ValueError):
                return None
            if moment is None:
                return None
            if moment.tzinfo is None:
                # An HTTP date without a usable zone is UTC by definition.
                moment = moment.replace(tzinfo=timezone.utc)
            seconds = (moment - datetime.now(timezone.utc)).total_seconds()
        if not math.isfinite(seconds):
            return None
        return max(0.0, seconds)

    @classmethod
    def _http_error(cls, response: httpx.Response) -> ProviderError:
        status = response.status_code
        if status == 401:
            kind = ProviderErrorKind.AUTHENTICATION
        elif status == 402:
            kind = ProviderErrorKind.BUDGET_EXCEEDED
        elif status == 403:
            kind = ProviderErrorKind.PERMISSION
        elif status == 429:
            kind = ProviderErrorKind.RATE_LIMIT
        elif status in {404, 410}:
            kind = ProviderErrorKind.MODEL_UNAVAILABLE
        elif status in {400, 405, 409, 415, 422}:
            kind = ProviderErrorKind.INVALID_REQUEST
        elif status >= 500:
            kind = ProviderErrorKind.PROVIDER_INTERNAL
        else:
            kind = ProviderErrorKind.INVALID_REQUEST
        retry_after = cls._retry_after(response.headers.get("Retry-After"))
        detail = response.reason_phrase or "provider request failed"
        return ProviderError(
            kind,
            f"HTTP {status}: {detail}",
            status_code=status,
            retry_after=retry_after,
        )
