"""OpenAI-compatible Chat Completions provider (streaming, tool calls, usage).

Works with OpenAI, DeepSeek, OpenRouter, LiteLLM gateways, vLLM, llama.cpp and the
Gemini OpenAI-compatible endpoint. Round-trips what such servers need back:
``reasoning_content`` (DeepSeek thinking + tools) and ``extra_content`` on tool calls
(Gemini ``thought_signature``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from karox6.core.messages import Message, ToolCall, Usage
from karox6.providers.base import (
    Delta,
    DeltaCallback,
    ErrorCode,
    ModelRequest,
    ModelResponse,
    ProviderError,
    is_empty,
)

if TYPE_CHECKING:
    import httpx

_REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")
_CONTEXT_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "context window",
)


@dataclass(slots=True)
class OpenAIChatProvider:
    base_url: str
    api_key: str | None = None
    name: str = "openai-chat"
    stream: bool = True
    timeout_s: float = 900.0
    headers: dict[str, str] = field(default_factory=dict)
    _client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            import httpx

            timeout = httpx.Timeout(self.timeout_s, connect=30.0)
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(to_wire(m) for m in request.messages)
        body: dict[str, Any] = {"model": request.model, "messages": messages}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if request.max_output_tokens:
            body["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning_effort:
            body["reasoning_effort"] = request.reasoning_effort
        if self.stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        body.update(request.extra_body)
        return body

    async def complete(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        import httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", **self.headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = self.build_body(request)
        acc = _Accumulator()
        try:
            if body.get("stream"):
                async with self._http().stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise _http_error(resp.status_code, resp.text, resp.headers)
                    try:
                        async for line in resp.aiter_lines():
                            acc.feed_sse_line(line, on_delta)
                            if acc.done:
                                break  # a reset after [DONE] must not discard the reply
                    except (httpx.TimeoutException, httpx.TransportError):
                        # The connection dropped after the reply finished (its finish
                        # reason arrived, only the usage chunk or [DONE] is missing): keep
                        # it rather than pay for the whole request again.
                        if acc.finish_reason is None:
                            raise
                    if acc.finish_reason is None:
                        # Also after [DONE]: a proxy may close the stream normally after
                        # its upstream dropped mid-reply; partial text is not an answer.
                        raise ProviderError(
                            "transport", "stream ended before the response finished"
                        )
            else:
                resp = await self._http().post(url, json=body, headers=headers)
                if resp.status_code >= 400:
                    raise _http_error(resp.status_code, resp.text, resp.headers)
                acc.feed_complete(resp.json())
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"request timed out: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise ProviderError("transport", f"connection failed: {exc!r}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("server", f"invalid JSON from server: {exc}") from exc
        response = acc.result()
        cap = body.get("max_tokens") or body.get("max_completion_tokens")  # as sent
        if cap and response.finish_reason != "length" and response.usage.output_tokens == cap:
            # Some routers report tool_calls (or stop) for a reply cut at max_tokens; its
            # last call's arguments are then incomplete, so treat it as the cut it is.
            response.finish_reason = "length"
        if is_empty(response.message) and response.finish_reason != "length":
            raise ProviderError("empty_response", "the model returned no text and no tool calls")
        return response


def to_wire(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content}
    if m.role == "assistant":
        # content null is valid only beside tool calls (a reasoning-only reply cut at the
        # token limit has neither).
        out: dict[str, Any] = {
            "role": "assistant",
            "content": (m.content or None) if m.tool_calls else m.content,
        }
        if m.tool_calls:
            out["tool_calls"] = []
            for call in m.tool_calls:
                wire: dict[str, Any] = {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments or "{}"},
                }
                if "extra_content" in call.extra:
                    wire["extra_content"] = call.extra["extra_content"]
                out["tool_calls"].append(wire)
        if m.reasoning:
            out[str(m.extra.get("reasoning_field") or "reasoning_content")] = m.reasoning
        return out
    return {"role": m.role, "content": m.content}


@dataclass(slots=True)
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class _Accumulator:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.reasoning_field: str | None = None
        self.calls: dict[int, _PartialCall] = {}
        self.finish_reason: str | None = None
        self.usage = Usage(requests=1)
        self.model: str | None = None
        self.done = False

    def feed_sse_line(self, line: str, on_delta: DeltaCallback | None) -> None:
        line = line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return
        chunk = json.loads(data)
        if isinstance(chunk, dict) and chunk.get("error"):
            err = chunk["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            kind = err.get("type") if isinstance(err, dict) else None
            text = str(message) if code is None or str(code).isdigit() else f"{code}: {message}"
            raise _http_error(_sse_error_status(code, kind), text, {})
        self._chunk(chunk, on_delta)

    def _chunk(self, chunk: dict[str, Any], on_delta: DeltaCallback | None) -> None:
        self.model = chunk.get("model") or self.model
        if chunk.get("usage"):
            self._usage(chunk["usage"])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.text.append(text)
                if on_delta:
                    on_delta(Delta("text", text))
            for name in _REASONING_FIELDS:
                thought = delta.get(name)
                if isinstance(thought, str) and thought:
                    self.reasoning.append(thought)
                    self.reasoning_field = self.reasoning_field or name
                    if on_delta:
                        on_delta(Delta("reasoning", thought))
                    break
            for part in delta.get("tool_calls") or []:
                self._call_delta(part)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def _call_delta(self, part: dict[str, Any]) -> None:
        index = int(part.get("index", len(self.calls)))
        call = self.calls.setdefault(index, _PartialCall())
        if part.get("id"):
            call.id = part["id"]
        fn = part.get("function") or {}
        name = fn.get("name")
        if name and name != call.name:
            # Most servers send the name once; a few repeat it in every chunk.
            call.name = name if not call.name else call.name + name
        if fn.get("arguments"):
            call.arguments.append(fn["arguments"])
        if part.get("extra_content"):
            call.extra["extra_content"] = part["extra_content"]

    def feed_complete(self, payload: dict[str, Any]) -> None:
        self.model = payload.get("model")
        if payload.get("usage"):
            self._usage(payload["usage"])
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("empty_response", "response has no choices")
        choice = choices[0]
        msg = choice.get("message") or {}
        if isinstance(msg.get("content"), str):
            self.text.append(msg["content"])
        for name in _REASONING_FIELDS:
            if isinstance(msg.get(name), str) and msg[name]:
                self.reasoning.append(msg[name])
                self.reasoning_field = name
                break
        for index, raw in enumerate(msg.get("tool_calls") or []):
            fn = raw.get("function") or {}
            call = _PartialCall(
                id=raw.get("id") or "",
                name=fn.get("name") or "",
                arguments=[fn.get("arguments") or ""],
            )
            if raw.get("extra_content"):
                call.extra["extra_content"] = raw["extra_content"]
            self.calls[index] = call
        self.finish_reason = choice.get("finish_reason")
        self.done = True

    def _usage(self, raw: dict[str, Any]) -> None:
        prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
        completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
        details = raw.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0)
        reasoning = int((raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        self.usage = Usage(
            input_tokens=prompt,
            cached_tokens=cached,
            output_tokens=completion,
            reasoning_tokens=reasoning,
            requests=1,
        )

    def result(self) -> ModelResponse:
        calls = []
        for n, index in enumerate(sorted(self.calls)):
            part = self.calls[index]
            if not part.name:
                continue
            calls.append(
                ToolCall(
                    id=part.id or f"call_{n}",
                    name=part.name,
                    arguments="".join(part.arguments),
                    extra=part.extra,
                )
            )
        extra: dict[str, Any] = {}
        if self.reasoning_field and self.reasoning_field != "reasoning_content":
            extra["reasoning_field"] = self.reasoning_field
        message = Message(
            "assistant",
            "".join(self.text),
            tool_calls=calls,
            reasoning="".join(self.reasoning) or None,
            extra=extra,
        )
        finish = _normalize_finish(self.finish_reason, bool(calls))
        return ModelResponse(
            message=message, usage=self.usage, finish_reason=finish, model=self.model
        )


def _sse_error_status(code: Any, kind: Any) -> int:
    """HTTP-like status for an error sent inside the stream; its code is often a string
    ("rate_limit_exceeded", "context_length_exceeded") rather than a number."""
    if str(code).isdigit():
        return int(str(code))
    label = f"{code} {kind}".lower()
    if "rate" in label or "quota" in label:
        return 429
    if any(hint in label for hint in _CONTEXT_HINTS) or "context" in label:
        return 400
    if "overload" in label:
        return 529
    return 500


def _normalize_finish(raw: str | None, has_calls: bool) -> str:
    if raw in ("length", "max_tokens"):
        return "length"
    if raw == "content_filter":
        return "content_filter"
    if has_calls:
        return "tool_calls"
    return "stop" if raw in (None, "stop", "end_turn", "tool_calls", "function_call") else "other"


def _http_error(status: int, text: str, headers: Any) -> ProviderError:
    snippet = " ".join(text.split())[:500]
    retry_after: float | None = None
    raw_retry = headers.get("retry-after") if hasattr(headers, "get") else None
    if raw_retry:
        try:
            retry_after = float(raw_retry)
        except ValueError:
            retry_after = None
    code: ErrorCode
    lowered = snippet.lower()
    if status == 429:
        code = "rate_limit"
    elif status in (401, 403):
        code = "auth"
    elif status in (408, 504):
        code = "timeout"
    elif any(hint in lowered for hint in _CONTEXT_HINTS):
        # Before the 5xx check: gateways wrap an upstream context overflow in a 500/502,
        # and retrying the same oversized request cannot succeed.
        code = "context_window"
    elif status >= 500 or status == 409:
        code = "server"
    else:
        code = "bad_request"
    return ProviderError(code, snippet or f"HTTP {status}", status=status, retry_after=retry_after)
