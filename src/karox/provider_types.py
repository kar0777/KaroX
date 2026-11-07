"""Provider data types, errors, tuning helpers and the Provider protocol."""

from __future__ import annotations

import base64
import json
import math
import re
import uuid
from dataclasses import dataclass, field
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
from urllib.parse import urlsplit
import httpx
from .security import contains_credential, redact


REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_REASONING_FAMILY = re.compile(r"^(?:o[1-9]\d*|gpt-5)(?:[-._]|$)")

def output_token_field(model: str) -> str:
    """Which field this model wants its output ceiling in."""

    name = model.rsplit("/", 1)[-1].strip().lower()
    return "max_completion_tokens" if _REASONING_FAMILY.match(name) else "max_tokens"

_ABOVE_HIGH_EFFORTS = frozenset({"xhigh", "max"})

def _effort_scale_stops_at_high(endpoint: str) -> bool:
    host = (urlsplit(endpoint).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".openai.azure.com")

CONNECT_TIMEOUT_SECONDS = 15.0

def transport_timeout(read_seconds: float, remaining_seconds: float) -> httpx.Timeout:
    """Split timeouts: a short connect/pool wait, the full budget between bytes.

    ``read`` is the idle gap allowed between two chunks of the stream, which a
    thinking model can legitimately stretch; connecting and acquiring a pooled
    connection should never take that long.
    """

    budget = max(0.05, min(float(read_seconds), float(remaining_seconds)))
    short = min(budget, CONNECT_TIMEOUT_SECONDS)
    return httpx.Timeout(budget, connect=short, pool=short, write=budget)

_GO_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")

def _go_duration_seconds(value: str) -> Optional[float]:
    """Parse ``1h2m3.5s`` / ``250ms`` / a bare number of seconds; None if not."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    position = 0
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}
    for match in _GO_DURATION_PART.finditer(text):
        if match.start() != position:
            return None
        total += float(match.group(1)) * scale[match.group(2)]
        position = match.end()
    return total if position == len(text) and position else None

def _looks_numeric(value: str) -> bool:
    try:
        float(str(value).strip())
    except ValueError:
        return False
    return True

def _accepts_prompt_cache_key(endpoint: str) -> bool:
    """Only OpenAI's own Chat Completions host documents ``prompt_cache_key``.

    A gateway that rejects the unknown field would trigger the optional-tuning
    retry, which also drops ``reasoning_effort`` and ``stream_options`` -- a
    weaker request and no usage report -- to gain nothing: other hosts cache by
    prefix without the routing hint.
    """

    host = (urlsplit(endpoint).hostname or "").lower()
    return host == "api.openai.com"

_OPTIONAL_TUNING_KEYS = (
    "reasoning_effort",
    "reasoning",
    "temperature",
    "prompt_cache_key",
    "parallel_tool_calls",
    "stream_options",
)

_REPLAYED_REASONING_KEY = "reasoning_content"

def _replays_reasoning(payload: Mapping[str, Any]) -> bool:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    return any(
        isinstance(item, Mapping) and _REPLAYED_REASONING_KEY in item
        for item in messages
    )

def _has_optional_tuning(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in _OPTIONAL_TUNING_KEYS) or _replays_reasoning(
        payload
    )

def _without_optional_tuning(payload: Mapping[str, Any]) -> Dict[str, Any]:
    reduced = {
        key: value for key, value in payload.items() if key not in _OPTIONAL_TUNING_KEYS
    }
    # `tool_choice: auto` is the default; removing it changes nothing, while a
    # `required` choice is part of the request's meaning and stays.
    if reduced.get("tool_choice") == "auto":
        reduced.pop("tool_choice", None)
    if _replays_reasoning(reduced):
        reduced["messages"] = [
            (
                {
                    key: value
                    for key, value in item.items()
                    if key != _REPLAYED_REASONING_KEY
                }
                if isinstance(item, Mapping)
                else item
            )
            for item in reduced["messages"]
        ]
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

_ERROR_BODY_LIMIT = 16_384

_VENDOR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")

def _vendor_error_codes(response: httpx.Response) -> tuple[str, ...]:
    """Machine-readable codes from a JSON error body, never its free text.

    "HTTP 400: Bad Request" does not say which part of the request was
    refused; the body's ``code``/``type``/``param`` usually do
    (``context_length_exceeded``, ``unsupported_parameter``, ``max_tokens``).
    Only short identifier-shaped strings from an error object are kept: the
    message, a bare string error or anything else free-form can echo the
    request, so it stays out of the error, the event log and the session.
    """

    try:
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) >= _ERROR_BODY_LIMIT:
                break
        body = json.loads(bytes(raw[:_ERROR_BODY_LIMIT]).decode("utf-8"))
    except (httpx.HTTPError, httpx.StreamError, UnicodeDecodeError, ValueError):
        return ()
    if not isinstance(body, dict):
        return ()
    error = body.get("error")
    if not isinstance(error, dict):
        # vLLM-style gateways put the error object at the top level.
        if body.get("object") != "error":
            return ()
        error = body
    codes: list[str] = []
    for key in ("code", "type", "param"):
        value = error.get(key)
        if (
            isinstance(value, str)
            and _VENDOR_CODE.fullmatch(value)
            and value.lower() != "error"
            and not contains_credential(value)
        ):
            text = value if key != "param" else f"param={value}"
            if text not in codes:
                codes.append(text)
    return tuple(codes)

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
    # Flat deliberation text from a gateway that streams it on its own key
    # (``reasoning_content``) with no signed blocks. Thinking models on the
    # Chat Completions wire (DeepSeek, Qwen and their hosts) expect it back on
    # the assistant turns of the current tool chain; it is never content.
    reasoning_text: Optional[str] = None

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
        if self.reasoning_text is not None and (
            self.role != "assistant" or not isinstance(self.reasoning_text, str)
        ):
            raise ValueError("only assistant messages may carry reasoning text")

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
