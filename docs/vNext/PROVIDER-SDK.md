# KaroX Provider SDK

Status: contract design; adapters are complete only when listed as such in
`IMPLEMENTATION_STATUS.md` and backed by contract/E2E tests.

## Contract

A provider adapter implements:

```python
class Provider:
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
    async def list_models(self) -> list[DiscoveredModel]: ...
    async def health(self) -> HealthResult: ...
```

`ModelRequest` contains normalized messages/content parts, available tool
schemas, tool-choice policy, model parameters, output/effort limits, deadline,
and correlation ID. It contains an opaque credential accessor, never a secret
that may be serialized.

`ModelEvent` is one of text delta, reasoning metadata, tool-call delta,
completed tool call, usage, provider notice, completion, or classified error.
The kernel reconstructs and validates tool calls; adapters never execute them.

## Error taxonomy

- `authentication`
- `permission`
- `rate_limit` with optional retry time
- `invalid_request`
- `unsupported_capability`
- `model_unavailable`
- `transport` (pre-response or interrupted stream)
- `provider_internal`
- `cancelled`
- `malformed_response`

Retries use taxonomy and request state. KaroX does not retry a tool mutation just
because a provider stream was interrupted.

## Provider and model records

Provider records include adapter kind, base URL, custom headers/query names,
credential reference, TLS/timeout/retry policy, privacy class, and health state.
Reserved authorization headers cannot be supplied in public metadata.

Model records include remote ID, aliases, context/output limits, tool/vision/
structured-output/streaming support, effort options, default parameters,
pricing, and source/provenance. Unknown capability remains `unknown`, never
optimistically `true`. Manual records work without an external catalog.

Profiles (`fast`, `balanced`, `strong`, `cheap`, `vision`, `coding`) select
models through explicit user-owned rules and do not change recorded capability.

## Generic OpenAI-compatible adapter

The common adapter targets Chat Completions and supports custom base URL,
headers, query parameters, model ID, temperature, tools, streaming, normalized
usage, timeout, and retry. Compatibility is verified per endpoint; naming a
service in examples does not make it tested.

Ollama, LM Studio, vLLM, OpenRouter, and other compatible endpoints should use
configuration profiles unless a contract test proves a semantic divergence that
requires code. OpenAI Responses, Anthropic Messages, and Gemini use dedicated
adapters because their event/content/tool semantics differ.

## Fallback and budget

Fallback requires:

- required model capabilities;
- session privacy/network class;
- remaining token/currency/time budgets;
- user-approved provider/model route;
- a handoff-safe request state.

Fallback events are visible and persisted. Cost is computed from provider usage
and versioned pricing metadata; unknown pricing is reported as unknown, not zero.

## Conformance suite

Each adapter must pass deterministic tests for text, multiple tool calls,
fragmented JSON arguments, cancellation, usage, timeouts, retryable errors,
authentication redaction, malformed events, and interrupted streams. A provider
is “tested” only after an opt-in live E2E records endpoint/model/date without
recording credentials.
