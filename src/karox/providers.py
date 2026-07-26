"""Provider transport contracts and the OpenAI-compatible adapter.

This module intentionally has no dependency on :mod:`karox.core`.  Providers
normalize model I/O; they never execute a local action or decide whether an
agent task succeeded.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from .security import contains_credential, redact


_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
    ) -> None:
        safe_message = str(redact(message))[:4000]
        super().__init__(f"{kind.value}: {safe_message}")
        self.kind = kind
        self.safe_message = safe_message
        self.status_code = status_code
        self.retry_after = retry_after
        self.route_attempts = tuple(dict(redact(item)) for item in route_attempts)


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
class ModelMessage:
    role: str
    content: Optional[str] = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported model message role: {self.role!r}")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("model message content must be text or null")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require a tool call ID")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages may contain tool calls")


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ProviderTool, ...] = ()
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    deadline_seconds: float = 120.0
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


@dataclass(frozen=True)
class ModelResponse:
    content: Optional[str]
    tool_calls: tuple[ToolCall, ...]
    finish_reason: Optional[str]
    usage: Dict[str, int]
    response_id: Optional[str] = None
    transport_attempts: int = 1
    route_attempts: tuple[Dict[str, Any], ...] = ()
    selected_provider: Optional[str] = None
    selected_model: Optional[str] = None
    cost: Optional[float] = None
    currency: Optional[str] = None
    pricing_version: Optional[str] = None
    cumulative_usage: Dict[str, int] = field(default_factory=dict)
    cumulative_cost: Optional[float] = None
    budget_exceeded: bool = False
    budget_reason: Optional[str] = None
    # Whatever reasoning the provider chose to expose, kept apart from
    # ``content`` so it is never mistaken for the model's answer.
    reasoning: Optional[str] = None


class ModelEventKind(str, Enum):
    TEXT_DELTA = "text_delta"
    # A model's own reasoning, kept on a separate channel from the answer.
    # Merging it into TEXT_DELTA would put private deliberation into the
    # assistant content that KaroX persists and re-sends as the answer.
    REASONING_DELTA = "reasoning_delta"
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
    tool_call_delta: Optional[ToolCallDelta] = None
    usage: Dict[str, int] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    response_id: Optional[str] = None
    transport_attempts: int = 1


class Provider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


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
    MAX_SSE_EVENTS = 10_000
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
        payload: Dict[str, Any] = {"role": message.role, "content": message.content}
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
            payload["tool_choice"] = "auto"
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
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
        headers = self._request_headers()
        deadline = time.monotonic() + float(request.deadline_seconds)
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
            try:
                with httpx.Client(follow_redirects=False) as client:
                    with client.stream(
                        "POST",
                        self.endpoint,
                        json=payload,
                        headers=headers,
                        timeout=min(self.timeout_seconds, max(remaining, 0.05)),
                    ) as response:
                        response_started = True
                        if response.status_code >= 400:
                            raise self._http_error(response)
                        try:
                            yield from self._stream_events(response, attempts, deadline)
                        except httpx.TransportError as exc:
                            raise ProviderError(
                                ProviderErrorKind.TRANSPORT,
                                f"provider stream interrupted: {type(exc).__name__}",
                                status_code=response.status_code,
                            ) from exc
                        return
            except ProviderError:
                raise
            except httpx.TransportError as exc:
                if response_started:
                    raise ProviderError(
                        ProviderErrorKind.TRANSPORT,
                        f"provider stream interrupted: {type(exc).__name__}",
                    ) from exc
                if attempts >= self.max_transport_retries + 1:
                    raise ProviderError(
                        ProviderErrorKind.TRANSPORT,
                        f"provider transport failed: {type(exc).__name__}",
                    ) from exc
                delay = self.retry_backoff_seconds * (2 ** (attempts - 1))
                if delay:
                    if delay >= deadline - time.monotonic():
                        raise ProviderError(
                            ProviderErrorKind.TRANSPORT,
                            "provider request deadline expired during retry",
                        ) from exc
                    time.sleep(delay)
                continue

    def complete(self, request: ModelRequest) -> ModelResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        saw_content = False
        call_parts: Dict[int, Dict[str, list[str]]] = {}
        usage: Dict[str, int] = {}
        finish_reason: Optional[str] = None
        response_id: Optional[str] = None
        transport_attempts = 1
        completed = False

        for event in self.stream(request):
            transport_attempts = event.transport_attempts
            if event.kind == ModelEventKind.TEXT_DELTA:
                saw_content = True
                content_parts.append(event.text_delta or "")
            elif event.kind == ModelEventKind.REASONING_DELTA:
                reasoning_parts.append(event.reasoning_delta or "")
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
                response_id = event.response_id

        if not completed:
            raise ProviderError(
                ProviderErrorKind.TRANSPORT,
                "provider stream ended before completion",
            )
        try:
            tool_calls = tuple(
                ToolCall(
                    call_id="".join(parts["call_id"]),
                    name="".join(parts["name"]),
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
        return ModelResponse(
            content="".join(content_parts) if saw_content else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            response_id=response_id,
            transport_attempts=transport_attempts,
            reasoning=reasoning or None,
        )

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

        for line in response.iter_lines():
            if time.monotonic() >= deadline:
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT,
                    "provider request deadline expired while reading stream",
                    status_code=response.status_code,
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
                if data.strip() == "[DONE]":
                    yield ModelEvent(
                        ModelEventKind.COMPLETION,
                        finish_reason=finish_reason,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                    return
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

        if data_lines:
            event_count += 1
            if event_count > cls.MAX_SSE_EVENTS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contains too many events",
                    status_code=response.status_code,
                )
            data = "\n".join(data_lines)
            if data.strip() == "[DONE]":
                yield ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason=finish_reason,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                return
            cls._parse_stream_chunk(data, response.status_code, attempts)
        raise ProviderError(
            ProviderErrorKind.TRANSPORT,
            "provider stream ended before [DONE]",
            status_code=response.status_code,
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
                for name in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                ):
                    if name not in raw_usage:
                        continue
                    raw_count = raw_usage[name]
                    if (
                        isinstance(raw_count, bool)
                        or not isinstance(raw_count, int)
                        or raw_count < 0
                    ):
                        raise ValueError(
                            "known usage field must be a non-negative integer"
                        )
                    usage[name] = raw_count
                events.append(
                    ModelEvent(
                        ModelEventKind.USAGE,
                        usage=usage,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                )

            choices = value.get("choices")
            if not isinstance(choices, list):
                raise ValueError("choices must be an array")
            if not choices:
                if raw_usage is None:
                    raise ValueError("event must contain a choice or usage")
                return events, None, response_id

            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("choice must be an object")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("choice delta must be an object")
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

            # Reasoning models on the OpenAI-compatible wire put their visible
            # thinking on a separate key. Dropping it left the user staring at a
            # blank screen for the whole thinking phase; both spellings are in
            # use across compatible vendors and gateways.
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

            raw_calls = delta.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise ValueError("tool-call deltas must be an array")
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
                if raw_type is not None and raw_type != "function":
                    raise ValueError("only function tool-call deltas are supported")
                call_id = raw.get("id", "")
                function = raw.get("function", {})
                if not isinstance(call_id, str) or not isinstance(function, dict):
                    raise ValueError("tool-call ID and function must be valid")
                name = function.get("name", "")
                arguments = function.get("arguments", "")
                if not isinstance(name, str) or not isinstance(arguments, str):
                    raise ValueError("tool-call name and arguments must be text")
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
