# KaroX vNext MCP Architecture

## One normalized MCP boundary

KaroX acts as both an MCP server and client. Incoming and outgoing tools are
normalized to a common descriptor, namespace, origin identity, capability set,
limits, and health state. Core policy is evaluated for every invocation.

## Server namespaces

Built-in tools use stable namespaces:

- `karox.repo.*`
- `karox.git.*`
- `karox.process.*`
- `karox.checks.*`
- `karox.browser.*`
- `karox.desktop.*`
- `karox.session.*`
- `karox.evidence.*`

Tool discovery returns only namespaces allowed for the authenticated bridge and
session. A later policy denial remains possible if arguments target a resource
outside the granted scope.

## Client transports

The client supports supervised stdio and Streamable HTTP first. Registrations
define executable/URL, non-secret environment names, credential references,
timeouts, restart policy, namespace, and disabled-by-default tool permissions.
OAuth is added per verified server where it is justified; it is not a generic
claim.

On connect, KaroX validates protocol version, tool schemas, unique names, size
limits, and cancellation behavior. Invalid or changed schemas quarantine that
server until re-approved. Stdio children receive a minimal environment and are
terminated with the session unless explicitly configured persistent.

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

A profile records client name, transport, authentication, tunnel/URL lifetime,
protocol quirks, connection instructions, doctor and handshake tests, verified
versions/dates, limitations, and one of `tested`, `experimental`,
`protocol-compatible`, or `planned`.

Current baseline:

| Profile | Status | Reason |
| --- | --- | --- |
| ChatGPT Web | experimental | OAuth discovery, DCR, PKCE, code exchange, refresh rotation/replay revocation, and authenticated MCP wire tests; no live workspace run |
| Claude Web | experimental | Same remote-MCP OAuth wire contract and official callback shape; no live account run |
| Notion | tested legacy | Existing provider/profile/transport/doctor tests |
| Generic Streamable HTTP | protocol-compatible vNext | Authenticated wire E2E covers built-in Core and proxied real stdio MCP tools |
| PromptQL | experimental | OpenAPI wire/Core E2E passes locally; no recorded live PromptQL product run |
| HyperAgent | experimental | No dedicated verified path in repository |

## Authentication and revocation

Each bridge session gets a high-entropy credential distinct from provider/MCP
credentials. Values are shown once through a protected channel and stored only
in the credential store. Rotation invalidates the prior value; emergency revoke
terminates tunnel access, mutation leases, and pending approvals.

Bearer profiles use that credential directly. OAuth web profiles use it only as
the human approval-page password; web clients receive short-lived access tokens
and rotating refresh tokens. Authorization codes are bound to the registered
client, exact redirect URI, public MCP resource, and PKCE verifier. Dynamic
clients and grants are intentionally process-local, so a bridge restart revokes
all issued OAuth state.

## Reliability

Transport reconnect never replays a mutation without the same Core idempotency
record. Hosted mutations must provide `_meta.karoxIdempotencyKey`; retrying with
the same key is safe, while omission fails closed. Downstream cancellation is
transport-dependent and is not claimed as verified. Health and schema changes
are visible session events.

## Verification gates

Contract tests cover discovery, invocation, errors, schema rejection,
namespaces, result limits, redaction, and idempotent replay. End-to-end tests
cover stdio native use, authenticated hosted built-in Core tools over both MCP
and OpenAPI, and an authenticated selected-tool external MCP proxy, including
permission denial, missing mutation idempotency, and live key rotation.
