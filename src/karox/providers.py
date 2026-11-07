"""Provider transport contracts and the OpenAI-compatible adapter.

This module intentionally has no dependency on :mod:`karox.core`.  Providers
normalize model I/O; they never execute a local action or decide whether an
agent task succeeded.
"""

from __future__ import annotations

from .provider_stream import ProviderStreamMixin

from .provider_types import (  # noqa: F401
    REASONING_EFFORTS as REASONING_EFFORTS,
    CONNECT_TIMEOUT_SECONDS as CONNECT_TIMEOUT_SECONDS,
    CredentialAccessor as CredentialAccessor,
    ImageAttachment as ImageAttachment,
    ModelEvent as ModelEvent,
    ModelEventKind as ModelEventKind,
    ModelMessage as ModelMessage,
    ModelRequest as ModelRequest,
    ModelResponse as ModelResponse,
    Provider as Provider,
    ProviderError as ProviderError,
    ProviderErrorKind as ProviderErrorKind,
    ProviderTool as ProviderTool,
    ReasoningBlock as ReasoningBlock,
    ToolCall as ToolCall,
    ToolCallDelta as ToolCallDelta,
    _ABOVE_HIGH_EFFORTS as _ABOVE_HIGH_EFFORTS,
    _ERROR_BODY_LIMIT as _ERROR_BODY_LIMIT,
    _GO_DURATION_PART as _GO_DURATION_PART,
    _OPTIONAL_TUNING_KEYS as _OPTIONAL_TUNING_KEYS,
    _REASONING_FAMILY as _REASONING_FAMILY,
    _REPLAYED_REASONING_KEY as _REPLAYED_REASONING_KEY,
    _TOOL_NAME as _TOOL_NAME,
    _VENDOR_CODE as _VENDOR_CODE,
    _accepts_prompt_cache_key as _accepts_prompt_cache_key,
    _effort_scale_stops_at_high as _effort_scale_stops_at_high,
    _go_duration_seconds as _go_duration_seconds,
    _has_optional_tuning as _has_optional_tuning,
    _join_identity_fragments as _join_identity_fragments,
    _looks_numeric as _looks_numeric,
    _replays_reasoning as _replays_reasoning,
    _usage_count as _usage_count,
    _vendor_error_codes as _vendor_error_codes,
    _without_optional_tuning as _without_optional_tuning,
    accumulate_response as accumulate_response,
    close_provider_quietly as close_provider_quietly,
    output_token_field as output_token_field,
    transport_timeout as transport_timeout,
)

import ipaddress
import math
import threading
import time
from dataclasses import replace
from typing import (
    Any,
    Dict,
    Iterator,
    Mapping,
    Optional,
)
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from .net_config import NetworkConfigError, client_options
from .security import contains_credential



# The reasoning-depth vocabulary KaroX exposes, ordered cheapest to most
# thorough. Each adapter maps these onto whatever its provider calls the same
# idea, and narrows the top of the range where a provider offers fewer levels.

# Chat Completions renamed the output cap for its reasoning families, and those
# models reject the old spelling with a 400 rather than ignoring it. The
# provider's error body is deliberately never read -- it can echo the request,
# credentials included -- so the spelling is chosen from the model family rather
# than from the rejection text. A gateway that renames the model past this check
# is covered by routing falling back to another route, not by a wider guess.




# The two KaroX levels above "high". OpenAI's own Chat Completions scale stops
# at "high", so OpenAI-hosted endpoints get those levels clamped there. Other
# OpenAI-compatible gateways publish their own scale -- W&B and DeepSeek accept
# "xhigh" and "max" -- so the level the user chose reaches them unchanged; a
# gateway that still rejects it gets one retry at "high" before any optional
# tuning field is dropped.




#: A host that does not answer the TCP/TLS handshake in this long is down or
#: unreachable; waiting the whole read budget (60 s by default, minutes for a
#: long deadline) for it only delays the retry or the error the user needs.












#: Optional tuning fields an OpenAI-compatible gateway may reject. A retry
#: without them keeps the required shape -- model, messages, tools,
#: tool_choice and the output cap -- so it is the same request, not a weaker
#: one. W&B DeepSeek is the case that made this necessary: it rejects
#: ``reasoning_effort=medium`` while accepting low/high/xhigh/max/none. Older
#: Azure api-versions and several proxies reject ``stream_options`` instead.


#: The message key that hands a thinking model its own earlier deliberation.
#: DeepSeek documents it as required on the assistant turns of a thinking-mode
#: tool chain; a gateway with a stricter message schema may refuse it, so it is
#: optional in the same sense as the tuning keys above.
















































_RESERVED_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key"}
)
_RESERVED_QUERY_NAMES = frozenset(
    {"access_token", "api_key", "apikey", "key", "token"}
)


class OpenAIChatCompletionsProvider(ProviderStreamMixin):
    """Synchronous streaming generic Chat Completions transport.

    Only failures before a usable HTTP response are retried.  A provider HTTP
    error or malformed response is surfaced once and is never reinterpreted as
    proof that a local mutation should be repeated.
    """

    MAX_SSE_LINE_CHARS = 1_048_576
    # Reasoning-heavy gateways can legitimately emit one tiny SSE frame per
    # reasoning token/fragment. Ox Alpha on ultra exceeded the old 10k ceiling
    # before producing its first tool call. The bounds follow the largest
    # answer a run may ask for: W&B frames one DeepSeek token in ~250 bytes of
    # JSON, so the old 16 MiB ceiling failed a single ~65k-token reply as
    # "malformed" -- a quarter of the 384k output cap the arena requests --
    # and ended the run. These still bound a pathological stream.
    MAX_SSE_EVENTS = 1_000_000
    MAX_SSE_TOTAL_CHARS = 201_326_592

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
                try:
                    network = client_options()
                except NetworkConfigError as exc:
                    raise ProviderError(ProviderErrorKind.TRANSPORT, str(exc)) from exc
                client = httpx.Client(
                    follow_redirects=False,
                    limits=httpx.Limits(
                        max_connections=100,
                        max_keepalive_connections=20,
                        keepalive_expiry=120.0,
                    ),
                    **network,
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
    def _message_payload(
        message: ModelMessage, *, replay_reasoning: bool = False
    ) -> Dict[str, Any]:
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
        if replay_reasoning and message.role == "assistant" and message.reasoning_text:
            payload[_REPLAYED_REASONING_KEY] = message.reasoning_text
        return payload

    @classmethod
    def _request_payload(cls, request: ModelRequest) -> Dict[str, Any]:
        # A thinking model's chat template keeps the deliberation of the turns
        # after the latest user message -- the tool chain in progress -- and
        # drops everything older. DeepSeek requires those turns' reasoning back
        # in thinking mode; without it each step saw its own earlier tool calls
        # stripped of the plan that produced them. Older turns would only be
        # discarded server-side, so they are not sent.
        last_user = max(
            (
                index
                for index, item in enumerate(request.messages)
                if item.role == "user"
            ),
            default=-1,
        )
        payload: Dict[str, Any] = {
            "model": request.model,
            "messages": [
                cls._message_payload(item, replay_reasoning=index > last_user)
                for index, item in enumerate(request.messages)
            ],
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
        if "prompt_cache_key" in payload and not _accepts_prompt_cache_key(self.endpoint):
            payload.pop("prompt_cache_key")
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
                    timeout=transport_timeout(self.timeout_seconds, remaining),
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




