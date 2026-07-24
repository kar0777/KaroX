"""Dedicated streaming adapters for non-Chat-Completions provider APIs.

The adapters normalize protocol-specific messages, tool calls, usage, and
errors into :mod:`karox.providers` contracts.  They deliberately share only
transport mechanics: protocol event handling stays explicit so an upstream API
change fails as a malformed response instead of being silently reinterpreted.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Dict, Iterator, Mapping, Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from .providers import (
    CredentialAccessor,
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    ModelResponse,
    OpenAIChatCompletionsProvider,
    ProviderError,
    ProviderErrorKind,
    ToolCall,
    ToolCallDelta,
)


class _StreamingAdapter:
    """Secret-safe HTTP/SSE transport and normalized response assembly."""

    provider_name = "provider"
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
        # Reuse the audited public-metadata validation from the compatible
        # adapter.  Authentication is added only after validation.
        self.base_url = self._base_url(base_url)
        self._credential = credential
        self._headers = OpenAIChatCompletionsProvider._validate_headers(headers or {})
        self._query = self._validate_query(query or {})
        if credential is not None and not OpenAIChatCompletionsProvider._credential_transport_is_secure(
            self.base_url
        ):
            raise ValueError(
                "provider credentials require HTTPS or a loopback HTTP endpoint"
            )
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

    @staticmethod
    def _base_url(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("provider base URL is required")
        parts = urlsplit(value.strip())
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
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")
        )

    @staticmethod
    def _validate_query(query: Mapping[str, str]) -> Dict[str, str]:
        # Building a dummy compatible endpoint applies the same secret checks.
        endpoint = OpenAIChatCompletionsProvider._endpoint(
            "https://validation.invalid", query
        )
        parsed = urlsplit(endpoint)
        # Values were already encoded by _endpoint.  Preserve the original
        # validated mapping instead of parsing and normalizing it again.
        assert parsed.query or not query
        return {str(name): str(value) for name, value in query.items()}

    def _secret(self) -> Optional[str]:
        if self._credential is None:
            return None
        try:
            value = self._credential()
        except Exception as exc:
            raise ProviderError(
                ProviderErrorKind.AUTHENTICATION,
                f"credential accessor failed: {type(exc).__name__}",
            ) from exc
        if not isinstance(value, str) or not value.strip():
            raise ProviderError(
                ProviderErrorKind.AUTHENTICATION,
                "credential accessor returned no API key",
            )
        if "\r" in value or "\n" in value:
            raise ProviderError(
                ProviderErrorKind.AUTHENTICATION,
                "credential contains invalid characters",
            )
        return value

    def _endpoint(self, request: ModelRequest) -> str:
        raise NotImplementedError

    def _request_payload(self, request: ModelRequest) -> Dict[str, Any]:
        raise NotImplementedError

    def _request_headers(self) -> Dict[str, str]:
        raise NotImplementedError

    def _events(
        self,
        response: httpx.Response,
        attempts: int,
        deadline: float,
    ) -> Iterator[ModelEvent]:
        raise NotImplementedError

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]:
        endpoint = self._endpoint(request)
        payload = self._request_payload(request)
        headers = self._request_headers()
        deadline = time.monotonic() + float(request.deadline_seconds)
        attempts = 0
        while True:
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT, "provider request deadline expired"
                )
            response_started = False
            try:
                with httpx.Client(follow_redirects=False) as client:
                    with client.stream(
                        "POST",
                        endpoint,
                        json=payload,
                        headers=headers,
                        timeout=min(self.timeout_seconds, max(remaining, 0.05)),
                    ) as response:
                        response_started = True
                        if response.status_code >= 400:
                            raise OpenAIChatCompletionsProvider._http_error(response)
                        try:
                            yield from self._events(response, attempts, deadline)
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
                if response_started or attempts >= self.max_transport_retries + 1:
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

    def complete(self, request: ModelRequest) -> ModelResponse:
        text: list[str] = []
        saw_text = False
        calls: Dict[int, Dict[str, list[str]]] = {}
        usage: Dict[str, int] = {}
        finish_reason: Optional[str] = None
        response_id: Optional[str] = None
        transport_attempts = 1
        completed = False
        for event in self.stream(request):
            transport_attempts = event.transport_attempts
            response_id = event.response_id or response_id
            if event.kind == ModelEventKind.TEXT_DELTA:
                saw_text = True
                text.append(event.text_delta or "")
            elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
                delta = event.tool_call_delta
                if delta is None:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "tool-call event omitted its delta",
                    )
                value = calls.setdefault(
                    delta.index, {"call_id": [], "name": [], "arguments": []}
                )
                value["call_id"].append(delta.call_id_fragment)
                value["name"].append(delta.name_fragment)
                value["arguments"].append(delta.arguments_fragment)
            elif event.kind == ModelEventKind.USAGE:
                usage.update(event.usage)
            elif event.kind == ModelEventKind.COMPLETION:
                completed = True
                finish_reason = event.finish_reason
        if not completed:
            raise ProviderError(
                ProviderErrorKind.TRANSPORT,
                "provider stream ended before completion",
            )
        try:
            tool_calls = tuple(
                ToolCall(
                    "".join(value["call_id"]),
                    "".join(value["name"]),
                    "".join(value["arguments"]),
                )
                for _, value in sorted(calls.items())
            )
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                f"invalid streamed tool call: {exc}",
            ) from exc
        return ModelResponse(
            content="".join(text) if saw_text else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            response_id=response_id,
            transport_attempts=transport_attempts,
        )

    @classmethod
    def _sse_records(
        cls, response: httpx.Response, deadline: float
    ) -> Iterator[tuple[Optional[str], str]]:
        event_name: Optional[str] = None
        data_lines: list[str] = []
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
                if data_lines:
                    event_count += 1
                    if event_count > cls.MAX_SSE_EVENTS:
                        raise ProviderError(
                            ProviderErrorKind.MALFORMED_RESPONSE,
                            "provider SSE stream contains too many events",
                            status_code=response.status_code,
                        )
                    yield event_name, "\n".join(data_lines)
                event_name, data_lines = None, []
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value if separator else "")
        if data_lines:
            event_count += 1
            if event_count > cls.MAX_SSE_EVENTS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contains too many events",
                    status_code=response.status_code,
                )
            yield event_name, "\n".join(data_lines)

    @staticmethod
    def _json_event(data: str, status_code: int) -> Dict[str, Any]:
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                "provider SSE event contained invalid JSON",
                status_code=status_code,
            ) from exc
        if not isinstance(value, dict):
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                "provider SSE event root must be an object",
                status_code=status_code,
            )
        return value

    @staticmethod
    def _usage(raw: Any, names: Mapping[str, str]) -> Dict[str, int]:
        if not isinstance(raw, dict):
            raise ValueError("usage must be an object")
        result: Dict[str, int] = {}
        for source, target in names.items():
            if source not in raw:
                continue
            value = raw[source]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError("known usage field must be a non-negative integer")
            result[target] = value
        return result

    def _url(self, suffix: str, extra_query: Optional[Mapping[str, str]] = None) -> str:
        parts = urlsplit(self.base_url)
        query = {**self._query, **(extra_query or {})}
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path.rstrip("/") + suffix,
                urlencode(query),
                "",
            )
        )


class OpenAIResponsesProvider(_StreamingAdapter):
    provider_name = "openai_responses"

    def _endpoint(self, request: ModelRequest) -> str:
        return self._url("/responses")

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **self._headers,
        }
        secret = self._secret()
        if secret is not None:
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    @staticmethod
    def _input(request: ModelRequest) -> list[Dict[str, Any]]:
        result: list[Dict[str, Any]] = []
        for message in request.messages:
            if message.role == "tool":
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content or "",
                    }
                )
                continue
            if message.content is not None:
                result.append({"role": message.role, "content": message.content})
            for call in message.tool_calls:
                result.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.raw_arguments,
                    }
                )
        return result

    def _request_payload(self, request: ModelRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": request.model,
            "input": self._input(request),
            "stream": True,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.input_schema,
                }
                for item in request.tools
            ]
            payload["tool_choice"] = "auto"
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        return payload

    def _events(
        self, response: httpx.Response, attempts: int, deadline: float
    ) -> Iterator[ModelEvent]:
        response_id: Optional[str] = None
        call_indexes: Dict[str, int] = {}
        call_output_indexes: set[int] = set()
        next_index = 0
        for event_name, data in self._sse_records(response, deadline):
            if data.strip() == "[DONE]":
                continue
            value = self._json_event(data, response.status_code)
            kind = value.get("type") or event_name
            if not isinstance(kind, str):
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "Responses event has no type",
                    status_code=response.status_code,
                )
            raw_response = value.get("response")
            if isinstance(raw_response, dict) and isinstance(raw_response.get("id"), str):
                response_id = raw_response["id"]
            if kind in {
                "response.created",
                "response.in_progress",
                "response.output_item.done",
                "response.content_part.added",
                "response.content_part.done",
                "response.output_text.done",
                "response.refusal.done",
                "response.function_call_arguments.done",
            }:
                continue
            if kind in {"response.output_text.delta", "response.refusal.delta"}:
                delta = value.get("delta")
                if not isinstance(delta, str):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Responses text delta must be text",
                    )
                yield ModelEvent(
                    ModelEventKind.TEXT_DELTA,
                    text_delta=delta,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                continue
            if kind == "response.output_item.added":
                item = value.get("item")
                if not isinstance(item, dict):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Responses output item must be an object",
                )
                if item.get("type") == "function_call":
                    item_id = item.get("id")
                    call_id = item.get("call_id")
                    name = item.get("name")
                    if (
                        (item_id is not None and not isinstance(item_id, str))
                        or not isinstance(call_id, str)
                        or not isinstance(name, str)
                    ):
                        raise ProviderError(
                            ProviderErrorKind.MALFORMED_RESPONSE,
                            "Responses function call requires ID and name",
                        )
                    index = value.get("output_index", next_index)
                    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                        raise ProviderError(
                            ProviderErrorKind.MALFORMED_RESPONSE,
                            "Responses output index must be non-negative",
                        )
                    aliases = {call_id}
                    if item_id:
                        aliases.add(item_id)
                    if index in call_output_indexes or any(
                        alias in call_indexes for alias in aliases
                    ):
                        raise ProviderError(
                            ProviderErrorKind.MALFORMED_RESPONSE,
                            "Responses function call has duplicate identity or index",
                        )
                    next_index = max(next_index, index + 1)
                    call_output_indexes.add(index)
                    for alias in aliases:
                        call_indexes[alias] = index
                    yield ModelEvent(
                        ModelEventKind.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(index, call_id, name, ""),
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                continue
            if kind == "response.function_call_arguments.delta":
                raw_aliases = (value.get("item_id"), value.get("call_id"))
                aliases = [alias for alias in raw_aliases if alias is not None]
                delta = value.get("delta")
                if (
                    not aliases
                    or any(not isinstance(alias, str) or alias not in call_indexes for alias in aliases)
                    or len({call_indexes[alias] for alias in aliases}) != 1
                    or not isinstance(delta, str)
                ):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Responses function arguments reference an unknown or conflicting call",
                    )
                yield ModelEvent(
                    ModelEventKind.TOOL_CALL_DELTA,
                    tool_call_delta=ToolCallDelta(
                        call_indexes[aliases[0]], arguments_fragment=delta
                    ),
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                continue
            if kind == "response.completed":
                if not isinstance(raw_response, dict):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Responses completion omitted response data",
                    )
                try:
                    usage = self._usage(
                        raw_response.get("usage"),
                        {
                            "input_tokens": "prompt_tokens",
                            "output_tokens": "completion_tokens",
                            "total_tokens": "total_tokens",
                        },
                    )
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "OpenAI Responses usage is invalid",
                    ) from exc
                if usage:
                    yield ModelEvent(
                        ModelEventKind.USAGE,
                        usage=usage,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                yield ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason="completed",
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                return
            if kind in {"response.failed", "error"}:
                raise ProviderError(
                    ProviderErrorKind.PROVIDER_INTERNAL,
                    "OpenAI Responses reported a failed response",
                    status_code=response.status_code,
                )
            if kind == "response.incomplete":
                reason = "incomplete"
                if isinstance(raw_response, dict):
                    details = raw_response.get("incomplete_details")
                    if isinstance(details, dict) and isinstance(details.get("reason"), str):
                        reason = details["reason"]
                yield ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason=reason,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                return
        raise ProviderError(
            ProviderErrorKind.TRANSPORT,
            "Responses stream ended before a terminal event",
            status_code=response.status_code,
        )


class AnthropicMessagesProvider(_StreamingAdapter):
    provider_name = "anthropic_messages"

    def _endpoint(self, request: ModelRequest) -> str:
        return self._url("/messages")

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            **self._headers,
        }
        secret = self._secret()
        if secret is not None:
            headers["x-api-key"] = secret
        return headers

    @staticmethod
    def _messages(request: ModelRequest) -> tuple[str, list[Dict[str, Any]]]:
        system: list[str] = []
        messages: list[Dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system.append(message.content)
                continue
            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                }
                if messages and messages[-1]["role"] == "user" and isinstance(
                    messages[-1]["content"], list
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue
            role = "assistant" if message.role == "assistant" else "user"
            blocks: list[Dict[str, Any]] = []
            if message.content is not None:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": json.loads(call.raw_arguments),
                }
                for call in message.tool_calls
            )
            messages.append({"role": role, "content": blocks})
        return "\n\n".join(system), messages

    def _request_payload(self, request: ModelRequest) -> Dict[str, Any]:
        try:
            system, messages = self._messages(request)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST,
                "persisted tool arguments are not valid JSON",
            ) from exc
        payload: Dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or 4096,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if request.tools:
            payload["tools"] = [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.input_schema,
                }
                for item in request.tools
            ]
            payload["tool_choice"] = {"type": "auto"}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    def _events(
        self, response: httpx.Response, attempts: int, deadline: float
    ) -> Iterator[ModelEvent]:
        response_id: Optional[str] = None
        finish_reason: Optional[str] = None
        message_started = False
        block_types: Dict[int, str] = {}
        stopped_blocks: set[int] = set()
        for event_name, data in self._sse_records(response, deadline):
            value = self._json_event(data, response.status_code)
            kind = event_name or value.get("type")
            if kind == "message_start":
                if message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message_start is duplicate",
                    )
                message = value.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message_start is invalid",
                    )
                message_started = True
                response_id = message["id"]
                try:
                    usage = self._usage(
                        message.get("usage", {}), {"input_tokens": "prompt_tokens"}
                    )
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic usage is invalid",
                    ) from exc
                if usage:
                    yield ModelEvent(
                        ModelEventKind.USAGE,
                        usage=usage,
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
                continue
            if kind == "content_block_start":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content block started before message_start",
                    )
                index = value.get("index")
                block = value.get("content_block")
                if isinstance(index, bool) or not isinstance(index, int) or not isinstance(block, dict):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content block start is invalid",
                    )
                if index < 0 or index in block_types:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content block index is duplicate or invalid",
                    )
                block_type = block.get("type")
                if block_type not in {"text", "tool_use"}:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "unsupported Anthropic content block",
                    )
                block_types[index] = block_type
                if block_type == "text":
                    initial_text = block.get("text", "")
                    if not isinstance(initial_text, str):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic text block is invalid")
                    if initial_text:
                        yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta=initial_text, response_id=response_id, transport_attempts=attempts)
                elif block_type == "tool_use":
                    call_id, name = block.get("id"), block.get("name")
                    if not isinstance(call_id, str) or not isinstance(name, str):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic tool block is invalid")
                    initial = block.get("input", {})
                    if not isinstance(initial, dict):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic tool input is invalid")
                    arguments = "" if initial == {} else json.dumps(initial, ensure_ascii=False, separators=(",", ":"))
                    yield ModelEvent(ModelEventKind.TOOL_CALL_DELTA, tool_call_delta=ToolCallDelta(index, call_id, name, arguments), response_id=response_id, transport_attempts=attempts)
                continue
            if kind == "content_block_delta":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content delta arrived before message_start",
                    )
                index, delta = value.get("index"), value.get("delta")
                if isinstance(index, bool) or not isinstance(index, int) or not isinstance(delta, dict):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic content delta is invalid")
                if index not in block_types or index in stopped_blocks:
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic content delta references an unknown block")
                delta_type = delta.get("type")
                expected_type = block_types[index]
                if delta_type == "text_delta" and expected_type == "text" and isinstance(delta.get("text"), str):
                    yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta=delta["text"], response_id=response_id, transport_attempts=attempts)
                elif delta_type == "input_json_delta" and expected_type == "tool_use" and isinstance(delta.get("partial_json"), str):
                    yield ModelEvent(ModelEventKind.TOOL_CALL_DELTA, tool_call_delta=ToolCallDelta(index, arguments_fragment=delta["partial_json"]), response_id=response_id, transport_attempts=attempts)
                else:
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic content delta does not match its block")
                continue
            if kind == "content_block_stop":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content stop arrived before message_start",
                    )
                index = value.get("index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or index not in block_types
                    or index in stopped_blocks
                ):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic content stop references an unknown block",
                    )
                stopped_blocks.add(index)
                continue
            if kind == "message_delta":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message delta arrived before message_start",
                    )
                delta = value.get("delta")
                if not isinstance(delta, dict):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic message delta is invalid")
                raw_reason = delta.get("stop_reason")
                if raw_reason is not None and not isinstance(raw_reason, str):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Anthropic stop reason is invalid")
                finish_reason = raw_reason
                try:
                    usage = self._usage(value.get("usage", {}), {"output_tokens": "completion_tokens"})
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic usage is invalid",
                    ) from exc
                if usage:
                    yield ModelEvent(ModelEventKind.USAGE, usage=usage, response_id=response_id, transport_attempts=attempts)
                continue
            if kind == "message_stop":
                if not message_started:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message stopped before message_start",
                    )
                if len(stopped_blocks) != len(block_types):
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Anthropic message stopped with open content blocks",
                    )
                yield ModelEvent(ModelEventKind.COMPLETION, finish_reason=finish_reason, response_id=response_id, transport_attempts=attempts)
                return
            if kind == "error":
                raise ProviderError(ProviderErrorKind.PROVIDER_INTERNAL, "Anthropic reported a stream error")
            if kind == "ping":
                continue
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                f"unsupported Anthropic event: {kind}",
            )
        raise ProviderError(ProviderErrorKind.TRANSPORT, "Anthropic stream ended before message_stop")


class GeminiGenerateContentProvider(_StreamingAdapter):
    provider_name = "gemini_generate_content"

    def _endpoint(self, request: ModelRequest) -> str:
        model = quote(request.model, safe="._-")
        return self._url(f"/models/{model}:streamGenerateContent", {"alt": "sse"})

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **self._headers,
        }
        secret = self._secret()
        if secret is not None:
            headers["x-goog-api-key"] = secret
        return headers

    @staticmethod
    def _contents(request: ModelRequest) -> tuple[Optional[Dict[str, Any]], list[Dict[str, Any]]]:
        systems: list[str] = []
        contents: list[Dict[str, Any]] = []
        names: Dict[str, str] = {}
        for message in request.messages:
            if message.role == "system":
                if message.content:
                    systems.append(message.content)
                continue
            if message.role == "tool":
                name = names.get(message.tool_call_id or "")
                if name is None:
                    raise ValueError("Gemini tool result references an unknown call")
                try:
                    parsed = json.loads(message.content or "null")
                except json.JSONDecodeError:
                    parsed = message.content or ""
                response_value = parsed if isinstance(parsed, dict) else {"result": parsed}
                part = {"functionResponse": {"name": name, "response": response_value}}
                if contents and contents[-1]["role"] == "user":
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            role = "model" if message.role == "assistant" else "user"
            parts: list[Dict[str, Any]] = []
            if message.content is not None:
                parts.append({"text": message.content})
            for call in message.tool_calls:
                names[call.call_id] = call.name
                parts.append(
                    {
                        "functionCall": {
                            "name": call.name,
                            "args": json.loads(call.raw_arguments),
                        }
                    }
                )
            contents.append({"role": role, "parts": parts})
        system = (
            {"parts": [{"text": "\n\n".join(systems)}]} if systems else None
        )
        return system, contents

    def _request_payload(self, request: ModelRequest) -> Dict[str, Any]:
        try:
            system, contents = self._contents(request)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST,
                "Gemini request contains invalid persisted tool data",
            ) from exc
        payload: Dict[str, Any] = {"contents": contents}
        if system is not None:
            payload["systemInstruction"] = system
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": item.name,
                            "description": item.description,
                            "parameters": item.input_schema,
                        }
                        for item in request.tools
                    ]
                }
            ]
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        generation: Dict[str, Any] = {}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            generation["maxOutputTokens"] = request.max_output_tokens
        if generation:
            payload["generationConfig"] = generation
        return payload

    def _events(
        self, response: httpx.Response, attempts: int, deadline: float
    ) -> Iterator[ModelEvent]:
        completed = False
        response_id: Optional[str] = None
        next_tool_index = 0
        for _event_name, data in self._sse_records(response, deadline):
            value = self._json_event(data, response.status_code)
            if isinstance(value.get("error"), dict):
                raise ProviderError(ProviderErrorKind.PROVIDER_INTERNAL, "Gemini reported a stream error")
            raw_id = value.get("responseId")
            if raw_id is not None:
                if not isinstance(raw_id, str):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini response ID is invalid")
                response_id = raw_id
            usage_raw = value.get("usageMetadata")
            if usage_raw is not None:
                try:
                    usage = self._usage(
                        usage_raw,
                        {
                            "promptTokenCount": "prompt_tokens",
                            "candidatesTokenCount": "completion_tokens",
                            "totalTokenCount": "total_tokens",
                        },
                    )
                except ValueError as exc:
                    raise ProviderError(
                        ProviderErrorKind.MALFORMED_RESPONSE,
                        "Gemini usage is invalid",
                    ) from exc
                yield ModelEvent(ModelEventKind.USAGE, usage=usage, response_id=response_id, transport_attempts=attempts)
            candidates = value.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                if usage_raw is not None:
                    continue
                raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini candidates must be a non-empty array")
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini candidate is invalid")
            content = candidate.get("content", {})
            if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
                raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini content is invalid")
            for part in content.get("parts", []):
                if not isinstance(part, dict):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini content part is invalid")
                if "text" in part:
                    if not isinstance(part["text"], str):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini text part is invalid")
                    yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta=part["text"], response_id=response_id, transport_attempts=attempts)
                if "functionCall" in part:
                    call = part["functionCall"]
                    if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("args"), dict):
                        raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini function call is invalid")
                    index = next_tool_index
                    next_tool_index += 1
                    call_id = f"gemini-call-{index}"
                    yield ModelEvent(
                        ModelEventKind.TOOL_CALL_DELTA,
                        tool_call_delta=ToolCallDelta(
                            index,
                            call_id,
                            call["name"],
                            json.dumps(call["args"], ensure_ascii=False, separators=(",", ":")),
                        ),
                        response_id=response_id,
                        transport_attempts=attempts,
                    )
            finish = candidate.get("finishReason")
            if finish is not None:
                if not isinstance(finish, str):
                    raise ProviderError(ProviderErrorKind.MALFORMED_RESPONSE, "Gemini finish reason is invalid")
                completed = True
                yield ModelEvent(ModelEventKind.COMPLETION, finish_reason=finish, response_id=response_id, transport_attempts=attempts)
                return
        if not completed:
            raise ProviderError(ProviderErrorKind.TRANSPORT, "Gemini stream ended before a finish reason")
