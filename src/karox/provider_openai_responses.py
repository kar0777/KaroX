"""OpenAI Responses streaming adapter."""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlsplit
import httpx
from .providers import (
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    ProviderError,
    ProviderErrorKind,
    ToolCallDelta,
    _REASONING_FAMILY,
)

from .provider_adapter_base import (
    _StreamingAdapter,
    _openai_effort,
    stream_error_kind,
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
            payload["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            # Reasoning models on the Responses wire reject temperature the
            # same way their Chat-Completions siblings do; the shared family
            # check keeps one preset from killing every gpt-5/o-series route.
            if not _REASONING_FAMILY.match(
                request.model.rsplit("/", 1)[-1].strip().lower()
            ):
                payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        host = (urlsplit(self.base_url).hostname or "").lower()
        if request.cache_key is not None and (
            host in {"api.openai.com", "chatgpt.com"} or host.endswith(".chatgpt.com")
        ):
            # OpenAI caches automatically; the key only routes requests sharing
            # a prefix to the same cache, which an agent loop always does. A
            # Responses-compatible gateway has no retry without the field, so
            # it is sent only where OpenAI documents it.
            payload["prompt_cache_key"] = request.cache_key
        if request.reasoning_effort is not None:
            payload["reasoning"] = {"effort": _openai_effort(request.reasoning_effort)}
        return payload

    def _events(
        self, response: httpx.Response, attempts: int, deadline: float
    ) -> Iterator[ModelEvent]:
        response_id: Optional[str] = None
        call_indexes: Dict[str, int] = {}
        call_output_indexes: set[int] = set()
        next_index = 0
        terminal_event: Optional[ModelEvent] = None
        for event_name, data in self._sse_records(response, deadline):
            if terminal_event is not None:
                # Protocol completion is known; consume the remaining HTTP body
                # without interpreting post-terminal frames so the socket can
                # return to the persistent client's connection pool.
                continue
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
                            "input_tokens_details.cached_tokens": "cache_read_tokens",
                            "output_tokens_details.reasoning_tokens": "reasoning_tokens",
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
                terminal_event = ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason="completed",
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                continue
            if kind in {"response.failed", "error"}:
                raise ProviderError(
                    stream_error_kind(
                        raw_response.get("error")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "OpenAI Responses reported a failed response",
                    status_code=response.status_code,
                )
            if kind == "response.incomplete":
                reason = "incomplete"
                if isinstance(raw_response, dict):
                    details = raw_response.get("incomplete_details")
                    if isinstance(details, dict) and isinstance(details.get("reason"), str):
                        reason = details["reason"]
                terminal_event = ModelEvent(
                    ModelEventKind.COMPLETION,
                    finish_reason=reason,
                    response_id=response_id,
                    transport_attempts=attempts,
                )
                continue
        if terminal_event is not None:
            yield terminal_event
            return
        raise ProviderError(
            ProviderErrorKind.TRANSPORT,
            "Responses stream ended before a terminal event",
            status_code=response.status_code,
        )
