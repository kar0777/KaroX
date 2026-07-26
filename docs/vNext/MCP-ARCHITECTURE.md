# KaroX vNext MCP Architecture

This file describes what `src/karox/mcp_client.py`, `proxy.py`, `proxy_server.py`,
`hosted_bridge.py` and `bridge.py` actually do. Anything not implemented is
marked **Planned** and must not be relied on.

## One normalized MCP boundary

KaroX acts as both an MCP server and client. Incoming and outgoing tools are
normalized to a common descriptor carrying a namespace, an origin identity, a
capability, and size limits. Core policy is evaluated for every invocation.

Health is **not** part of that descriptor. Reachability is observable only
through a deliberate probe surfaced by `karox.mcp_status`; without a probe the
state is `not_probed`, never `ok`. Authorization and reachability are separate
fields there on purpose.

## Server namespaces

The built-in tools a hosted client can be granted are exactly these, from
`hosted_bridge.CORE_TOOL_NAMES`:

- `karox.repo.*` — `read_file`, `read_lines`, `write_file`, `edit_file`,
  `list_files`, `search`
- `karox.git.*` — `status`, `diff`, `log`, `commit`
- `karox.checks.*` — `run`

**Planned:** `karox.process.*`, `karox.browser.*`, `karox.desktop.*`,
`karox.session.*` and `karox.evidence.*` do not exist. Earlier versions of this
document listed all eight namespaces as stable, which made five of them look
grantable. Session and evidence data is reachable over the OpenAPI connector's
`/session` and `/context/brief` endpoints, but those are HTTP endpoints, not MCP
tool namespaces.

Tool discovery returns only the tools explicitly allowlisted for the
authenticated bridge and session. A later policy denial remains possible if
arguments target a resource outside the granted scope.

## Client transports

The client supports supervised stdio and Streamable HTTP. `McpServerRecord`
defines the executable and arguments or the URL, non-secret environment names,
headers, a credential reference with its target header and scheme, read-only tool
names, a namespace, `timeout_seconds`, `max_result_bytes`, `max_message_bytes`,
and `max_transport_retries`. Tool permissions default to `ask`, which blocks the
call until the session selection is edited — there is no interactive prompt.

`max_transport_retries` is a **transport retry count**, not a restart policy: a
failed discovery or call is re-attempted that many times within one operation.
There is no supervision policy, no backoff configuration, and no restart
bookkeeping for a stdio child.

OAuth is added per verified server where it is justified; it is not a generic
claim.

On connect, KaroX validates the advertised tool set: safe tool names, a strict
JSON object input schema per tool, unique namespaced names, configured read-only
names that actually exist, and the discovery response size against
`max_message_bytes`. Protocol version negotiation is whatever the installed MCP
SDK's `initialize` performs; KaroX adds no check of its own.

**Not implemented:** cancellation behaviour is not probed on connect, and there
is no quarantine flag. What does happen when a schema changes is narrower and
worth stating exactly: the registry digest and each tool's schema digest are
recorded in the session selection, and a change invalidates it — the selection
reports `stale` and every tool in it becomes `blocked_stale` until it is
re-approved. A tool whose schema changed does not inherit its old permission. An
invalid schema is rejected outright at discovery, so the server never becomes
usable rather than being held in a quarantine state.

Stdio children receive a minimal environment built from the record's declared
non-secret names, and are terminated with the operation that started them.

**Planned:** a persistent stdio option. There is no configuration that keeps a
stdio child alive beyond its session; the earlier "unless explicitly configured
persistent" wording described a setting that does not exist.

## Proxy flow

```text
hosted client
  -> authenticated bridge profile
  -> session + hosted-client origin policy
  -> explicit proxy allowlist
  -> normalized external MCP tool
  -> external-MCP origin policy
  -> supervised MCP transport
```

Both the hosted client and external server policies must allow the call. Proxy
descriptors never include provider keys, MCP credentials, process environment,
or tools outside the allowlist. Results are size-limited and redacted before
returning to the hosted client.

Built-in tools use a shorter path because there is no external MCP trust
boundary:

```text
hosted client
  -> Bearer/X-API-Key bridge authentication
  -> explicit --tool allowlist + hosted-client origin policy
  -> Core Runtime command + session profile
  -> repository/check/Git implementation
```

The same allowlist is exposed either as Streamable HTTP MCP (`/mcp`) or as an
OpenAPI 3.1 connector (`/openapi.json`). OpenAPI exposes authenticated
`/session` and `/context/brief` preflight endpoints. Mutations require a stable
idempotency key in MCP request `_meta.karoxIdempotencyKey` or the OpenAPI
`X-KaroX-Idempotency-Key` header.

## Bridge profiles

A profile records client name, transport, authentication scheme, tunnel kind,
whether the URL is persistent, connection instructions, limitations, verified
versions, and one of `tested`, `tested_legacy`, `experimental`,
`protocol_compatible`, or `planned`. `tested` and `tested_legacy` both require
non-empty verified versions.

A profile is metadata, not an integration. It does not record doctor or handshake
tests, protocol quirks, or URL lifetimes; those were never fields.

`tested_legacy` exists because "we have a test" and "we have a test of *this*
runtime" are different facts. It means the evidence exercises the legacy
`server/` gateway — a different HTTP server — and says nothing about
`src/karox`. It still counts as usable, on the same ground as
`protocol_compatible`: the wire itself is covered end to end by this runtime's
own tests.

Current baseline:

| Profile | Status | Reason |
| --- | --- | --- |
| ChatGPT Web | experimental | OAuth discovery, DCR, PKCE, code exchange, refresh rotation/replay revocation, and authenticated MCP wire tests; no live workspace run |
| Claude Web | experimental | Same remote-MCP OAuth wire contract and official callback shape; no live account run |
| Notion | tested_legacy | `scripts/test_notion_mcp_transport.py` drives `server/notion_gateway.py`; no recorded Notion run against the vNext bridge |
| Generic Streamable HTTP | protocol_compatible | Authenticated wire E2E covers built-in Core and proxied real stdio MCP tools |
| PromptQL | experimental | OpenAPI wire/Core E2E passes locally; no recorded live PromptQL product run |
| HyperAgent | experimental | No dedicated verified path in repository |

No profile is `tested`. That label is reserved for a recorded end-to-end run
against this runtime, and none has been recorded yet.

## Authentication and revocation

Each bridge session gets a high-entropy credential distinct from provider/MCP
credentials. Values are shown once and stored only in the credential store.
Rotation invalidates the prior value; revocation deletes it.

Bearer profiles use that credential directly. OAuth web profiles use it only as
the human approval-page password; web clients receive short-lived access tokens
and rotating refresh tokens. Authorization codes are bound to the registered
client, exact redirect URI, public MCP resource, and PKCE verifier. Dynamic
clients and grants are intentionally process-local, so a bridge restart revokes
all issued OAuth state.

## Reliability

Transport reconnect never replays a mutation without the same Core idempotency
record. Hosted mutations must provide `_meta.karoxIdempotencyKey`; retrying with
the same key is safe, while omission fails closed. A mutating call with an
unknown outcome raises `McpUnknownOutcome` instead of being retried.

Downstream cancellation is transport-dependent and is not implemented or
verified. Schema changes are visible in the session selection as described under
*Client transports*; health changes are visible only when `mcp status` probes.

## Verification gates

Contract tests cover discovery, invocation, errors, schema rejection,
namespaces, result limits, redaction, and idempotent replay. End-to-end tests
cover stdio native use, authenticated hosted built-in Core tools over both MCP
and OpenAPI, and an authenticated selected-tool external MCP proxy, including
permission denial, missing mutation idempotency, and live key rotation.
