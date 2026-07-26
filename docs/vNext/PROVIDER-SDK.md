# KaroX Provider SDK

Status: this file describes the contract as it exists in `src/karox/providers.py`
and `src/karox/provider_adapters.py`. Anything not yet implemented is marked
**Planned** and is not available to write an adapter against. Adapters are
complete only when listed as such in `IMPLEMENTATION_STATUS.md` and backed by
contract/E2E tests.

## Contract

A provider adapter implements `karox.providers.Provider`:

```python
class Provider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...
```

The transport is **synchronous**: `stream` returns a plain iterator, not an async
one. `complete` folds a stream into one `ModelResponse` through
`accumulate_response`, so a streaming caller and a blocking caller can never
disagree about the same bytes.

**Planned, not implemented:** `list_models` and `health` are not part of the
protocol and no adapter provides them. Model discovery today is a TUI-side
routine (`karox.tui._discover_models`) that reads a provider's model-listing
endpoint during `/connect`; there is no `DiscoveredModel` or `HealthResult` type
in the provider layer, and reachability is not a provider-contract method. The
comparable observable state that does exist is the MCP liveness probe in
`karox.mcp_status`, which is a different subsystem.

### `ModelRequest`

The real fields are `model`, `messages`, `tools`, `temperature`,
`max_output_tokens`, `deadline_seconds`, `cache_key`, `reasoning_effort`, and
`correlation_id`.

- Messages are `ModelMessage` records with a **text** `content` field, plus
  assistant `tool_calls` and a `tool_call_id` for tool results. There is no
  multi-part content model; images and other parts are not representable.
- `reasoning_effort` is one of `low`, `medium`, `high`, `xhigh`, `max`, and each
  adapter maps it onto its provider's own name for the same idea.
- `cache_key` is opt-in prompt-cache identity, because a one-shot request would
  pay the cache-write premium and never read it back.

**Not present:** there is no tool-choice policy field. An adapter sets
`tool_choice` to the provider's "auto" spelling whenever the request carries
tools, and the caller cannot override that. There is also no credential accessor
on the request: the accessor is a constructor argument to the adapter, supplied
by `karox.provider_factory` from the credential store. Secrets never travel in
`ModelRequest`, which is what the original wording was reaching for.

### `ModelEvent`

`ModelEventKind` has exactly five members:

- `text_delta`
- `reasoning_delta` — the model's own deliberation, kept off the answer channel
  so it is never persisted or re-sent as assistant content
- `tool_call_delta`
- `usage`
- `completion` — carries the authoritative `ModelResponse` for the call

The kernel reconstructs and validates tool calls from the deltas; adapters never
execute them.

**Not present:** there is no completed-tool-call event, no provider-notice
event, and no error event. A classified failure is raised as `ProviderError`,
not yielded, so a consumer must catch it rather than match on an event kind.

## Error taxonomy

`ProviderErrorKind`:

- `authentication`
- `permission`
- `rate_limit` — with optional `retry_after`
- `invalid_request`
- `unsupported_capability`
- `model_unavailable`
- `transport` (pre-response or interrupted stream)
- `provider_internal`
- `cancelled`
- `malformed_response`
- `budget_exceeded`

Only `transport` failures that happen before a usable HTTP response are retried.
KaroX does not retry a tool mutation because a provider stream was interrupted.

`cancelled` is a **declared but unreached** classification: nothing in
`src/karox` raises it, because the synchronous transport has no cancellation
path. Do not treat it as an implemented capability.

## Provider and model records

`ProviderRecord` holds `adapter_kind`, `base_url`, custom `headers` and `query`
names, `credential_ref`, `privacy_class`, `timeout_seconds`, and
`max_transport_retries`. Reserved authorization headers and credential-shaped
query names cannot be supplied in public metadata.

**Not present:** no TLS policy and no health-state field. Transport behaviour is
the timeout and the retry count, and nothing else.

`ModelRecord` holds `model_id`, `aliases`, `context_window`,
`max_output_tokens`, the `tools` / `vision` / `structured_output` / `streaming`
capability flags, `pricing`, and `provenance`. An unknown capability stays
`unknown` and is never optimistically promoted to `true`. Manual records work
without an external catalog.

**Not present:** there is no effort-options field and no default-parameters
field on a model record. Effort is chosen per request.

**Planned:** named selection profiles. There is no `fast` / `balanced` /
`strong` / `cheap` / `vision` / `coding` vocabulary anywhere in the runtime.
Routing is an explicit ordered `RoutingPolicy.routes` tuple of
`(provider_id, model)` targets that the user configures.

## Generic OpenAI-compatible adapter

The common adapter targets Chat Completions and supports custom base URL,
headers, query parameters, model ID, temperature, tools, streaming, normalized
usage, timeout, and retry. Compatibility is verified per endpoint; naming a
service in examples does not make it tested.

Ollama, LM Studio, vLLM, OpenRouter, and other compatible endpoints should use
configuration presets (`karox.provider_presets`) unless a contract test proves a
semantic divergence that requires code. OpenAI Responses, Anthropic Messages,
and Gemini use dedicated adapters because their event/content/tool semantics
differ.

## Fallback and budget

`RoutingPolicy` gates a fallback on:

- `require_tools` and `require_streaming` against the recorded model capability —
  those two flags only, not an arbitrary capability set;
- `privacy_limit` against the provider's `privacy_class`;
- `max_total_tokens`, and `max_cost` in a named `currency`.

**Not present:** there is no time budget in the routing policy — a deadline is
per request (`ModelRequest.deadline_seconds`) — and no handoff-safe request-state
precondition on fallback. Routes are user-configured, which is what makes them
"approved"; there is no separate per-route approval record.

Route attempts travel on `ModelResponse.route_attempts` and are persisted,
redacted, into the session's `provider_history`. Cost is computed from provider
usage and versioned pricing metadata; unknown pricing is reported as unknown,
not zero.

## Conformance suite

`tests/test_providers.py` and `tests/test_provider_adapters.py` cover text,
multiple tool calls, fragmented JSON arguments, usage normalization, timeouts,
retryable errors, authentication redaction, malformed events, and interrupted
streams.

Two items the earlier version of this list claimed are **not** covered, because
neither exists to test: cancellation (see the error taxonomy above), and the
opt-in live end-to-end run. No adapter has recorded a live endpoint/model/date,
so no provider in this tree is "tested" in that stronger sense.
