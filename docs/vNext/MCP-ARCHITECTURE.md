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

## Bridge profiles

A profile records client name, transport, authentication, tunnel/URL lifetime,
protocol quirks, connection instructions, doctor and handshake tests, verified
versions/dates, limitations, and one of `tested`, `experimental`,
`protocol-compatible`, or `planned`.

Current baseline:

| Profile | Status | Reason |
| --- | --- | --- |
| Notion | tested legacy | Existing provider/profile/transport/doctor tests |
| Generic Streamable HTTP | protocol-compatible legacy | Transport and host-security tests; client-dependent |
| PromptQL | experimental | Launcher/OpenAPI integration lacks dedicated E2E |
| HyperAgent | experimental | No dedicated verified path in repository |

## Authentication and revocation

Each bridge session gets a high-entropy credential distinct from provider/MCP
credentials. Values are shown once through a protected channel and stored only
in the credential store. Rotation invalidates the prior value; emergency revoke
terminates tunnel access, mutation leases, and pending approvals.

## Reliability

Transport reconnect never replays a mutation without the same Core idempotency
record. Tool cancellation propagates to Core/MCP when supported and otherwise
marks the outcome unknown for reconciliation. Health and schema changes are
visible session events.

## Verification gates

Contract tests cover discovery, invocation, errors, cancellation, reconnect,
schema rejection, namespaces, result limits, and redaction. End-to-end tests
cover stdio native use, hosted selected-tool proxy use, permission denial, and
interrupted mutation without duplication.
