# KaroX 5 connectivity

KaroX supports three different connection directions. They share the same Core
policy boundary but are not interchangeable.

## 1. KaroX native agent to a model API

The local agent calls a configured model provider. Model tool calls return to
KaroX, and every local action executes through Core.

Supported adapter contracts:

- OpenAI Responses;
- Anthropic Messages;
- Gemini GenerateContent;
- generic OpenAI-compatible Chat Completions with streaming.

Credentials are stored in the OS keyring. Registry and session files contain
only opaque references.

Typical setup is performed through `/connect` in the TUI. The scriptable surface
uses the `credential`, `provider`, `model`, and `agent` commands. Always inspect
the installed command help before copying advanced options from a development
document:

```bash
karox credential --help
karox provider --help
karox model --help
karox agent --help
```

A route is activated only after the selected connection test succeeds. KaroX
must not silently change provider, cost boundary, privacy class, or mutation
behavior.

## 2. Hosted client to KaroX

A hosted client calls an explicit allowlist of built-in Core tools, selected
external MCP tools, or both. Nothing is exposed automatically.

### ChatGPT Web

Normal entry point:

```bash
karox bridge connect chatgpt-web --repository . --write
```

Without `--write`, the bridge exposes a read-only repository and Git tool set.
The command creates a repository-bound session, temporary approval credential,
local MCP server, and managed HTTPS tunnel, then prints:

- the MCP URL;
- the OAuth approval password;
- connection instructions;
- the session identifier;
- a warning when the public URL is temporary.

Create the custom/developer MCP app in ChatGPT, paste only the MCP URL into its
configuration, and complete authorization in the browser. Enter the approval
password only on the KaroX approval page.

The local OAuth and MCP contract is tested. A real ChatGPT workspace run remains
pending until `docs/conformance/chatgpt-web.md` records a passed result.

### Claude Web

Normal entry point:

```bash
karox bridge connect claude-web --repository . --write
```

Add the printed MCP URL as a custom Claude connector and complete KaroX OAuth
approval. The same read-only default, explicit write opt-in, session binding,
and Core policy apply.

The local OAuth and MCP contract is tested. A real Claude paid-account run
remains pending until `docs/conformance/claude-web.md` records a passed result.

### HyperAgent

Normal entry point:

```bash
karox bridge connect hyperagent-web --repository . --write
```

HyperAgent connects to KaroX as a remote MCP client over OAuth, the same
protocol ChatGPT and Claude use. A stable public HTTPS URL is required — the
recommended way is Tailscale Funnel:

```bash
karox bridge connect hyperagent-web --repository "D:\проекты\KaroX-v5" --tunnel tailscale
```

The command prints the MCP URL (ending in `/mcp`), the OAuth approval password,
and step-by-step HyperAgent instructions. In HyperAgent:

1. Open **Add MCP server**.
2. Set **Name** to `KaroX`.
3. Set **URL** to the printed MCP URL (it ends in `/mcp`).
4. Leave **Bring my own OAuth app** disabled. KaroX publishes OAuth discovery
   metadata and supports Dynamic Client Registration, so HyperAgent registers
   its own client and you never enter a Client ID or Client Secret by hand.
5. Enable **I trust this server** only if you trust this local KaroX instance.
6. Click **Connect**.
7. On the KaroX page that opens, paste the approval password and click
   **Authorize**.

KaroX pins the HyperAgent redirect host (`hyperagent.com`) for this profile, so
an authorization code can only be sent back to HyperAgent — not to an arbitrary
HTTPS server. The approval password is the KaroX-side gate on the consent page;
it is never a Client Secret and never reaches HyperAgent.

A saved reusable profile works the same way as for the other web clients:

```bash
karox bridge saved create hyperagent-dev --target-profile hyperagent-web --repository "D:\проекты\KaroX-v5" --tunnel tailscale --language ru
karox bridge connect --saved hyperagent-dev
```

The local OAuth, DCR, PKCE, and redirect-host-allowlist contract is tested. A
real HyperAgent workspace run remains pending until
`docs/conformance/hyperagent-web.md` records a passed result — until then the
profile status is `experimental`, not `live verified`.

### Saved connection profiles

A saved profile stores only non-secret launch policy: connector target,
repository path, tool allowlist, verification-command allowlist, deadline,
tunnel mode, preferred language, access profile, port, and tunnel timeout.
Credentials and OAuth grants remain in the OS keyring and runtime stores.

Create and validate a reusable development profile:

```bash
karox bridge saved create full-dev --target-profile chatgpt-web --repository . --write --tool karox.repo.read_file --tool karox.repo.read_lines --tool karox.repo.list_files --tool karox.repo.search --tool karox.git.status --tool karox.git.diff --tool karox.git.log --tool karox.checks.run --verification-command '["python","scripts/run_v5_preflight.py","--apply-reviewed-fixes","--full","--keep-going"]' --deadline-preset full-suite --tunnel tailscale --language ru
karox bridge saved validate full-dev --json
```

Windows PowerShell can remove embedded JSON quotes before a native program sees
them. KaroX accepts canonical JSON, backslash-escaped JSON such as
`[\"python\",\"-m\",\"pytest\"]`, and the simple quote-stripped native form
`[python,-m,pytest]`. It parses the value at `bridge connect`, stores a typed
`list[str]` command, and serializes canonical JSON for the child bridge.

After initial setup, reconnect with:

```bash
karox bridge connect --saved full-dev
```

Use `karox bridge saved list`, `show`, `edit`, `validate`, and `delete` to manage
profiles. `--diagnostics-only` prints the effective contract without starting a
listener or public tunnel.

### Deadlines and diagnostics

`--deadline-preset standard`, `long`, and `full-suite` map to 600, 1800, and 3600
seconds. An explicit `--deadline-seconds` is also accepted up to the same
3600-second limit used by the actual MCP/OpenAPI execution path. KaroX reports
requested and effective values and warns before launch when a known full-suite
verification command is likely to exceed the selected deadline.

The managed launcher prints a non-secret JSON diagnostic object. Authenticated
clients can also read it through the MCP tool `karox.bridge.diagnostics` or the
OpenAPI endpoint `/diagnostics`. It reports available tools, disabled tools and
reasons, verification allowlists, effective deadline, tunnel type, URL
stability, session identifier, and session lifetime. The diagnostic surface does
not contain bridge credentials, OAuth passwords, access tokens, refresh tokens,
or provider keys.

### OAuth requirements

The web bridge implements:

- protected-resource and authorization-server discovery;
- Dynamic Client Registration for public clients;
- Authorization Code with PKCE S256;
- exact client, redirect, and resource binding;
- short-lived access tokens;
- rotating refresh tokens;
- token-family revocation on refresh replay;
- a local password-protected KaroX approval page.

Authentication does not grant tools. Tool selection and capabilities are still
evaluated by Core for every call.

### Temporary and stable URLs

A Cloudflare Quick Tunnel URL changes after restart. The connector saved by the
hosted client then points to an address that no longer exists, and grants bound
to the previous resource cannot be reused on the new origin.

Tailscale Funnel can provide the node's stable `.ts.net` hostname:

```bash
karox bridge connect chatgpt-web --repository . --tunnel tailscale
```

KaroX checks installation, login state, machine approval, MagicDNS hostname, and
existing Serve/Funnel ownership before mutation. It refuses to replace an
existing or unclassifiable route. The managed flow uses a foreground child and
never performs a global Serve/Funnel reset, so cleanup stops only the route owned
by the current KaroX launcher. The listener remains loopback-only.

The local contract is unit-tested without an account. Real Funnel policy,
certificate, stable-hostname, and cleanup behavior remain pending until
[`TAILSCALE_LIVE_RUNBOOK.md`](TAILSCALE_LIVE_RUNBOOK.md) records a real run.

A user-managed stable HTTPS origin remains available through the custom tunnel
options shown by:

```bash
karox bridge connect --help
```

Do not weaken origin, redirect, or resource validation to preserve a stale
connector.

### Generic Streamable HTTP MCP

The lower-level bridge can expose selected Core and external MCP tools over an
authenticated Streamable HTTP endpoint. Use the `bridge serve` help to configure
profile, protocol, repository, session, credential, and tool allowlists:

```bash
karox bridge serve --help
```

MCP mutations require a stable idempotency key. A client that retries the same
call must receive the recorded outcome rather than duplicate the mutation.

### Generic OpenAPI bridge

The OpenAPI bridge exposes explicitly selected built-in Core tools plus session
and context endpoints. Mutations require a caller-generated stable idempotency
header. The server binds to loopback by default; do not publish a clear-text raw
HTTP port to the internet.

A non-loopback deployment requires an intentionally configured TLS termination
or secure reverse proxy. Store credentials in the hosted product's protected
credential field, never in the schema, URL, or chat.

## 3. KaroX CLI to a hosted agent API

A hosted agent with a documented invocation API is a target, not a local
model-provider adapter. KaroX currently implements this direction for PromptQL's
Natural Language API contract.

PromptQL executes actions against its own hosted data sources and returns
assistant actions rather than local tool calls. KaroX therefore exposes it under
a dedicated `target ask` flow. It must not be inserted into the native local
tool-calling loop as if it were an OpenAI-compatible provider.

The PromptQL contract has deterministic mocked-HTTP coverage, but no passed live
product record. It remains Experimental.

## External MCP servers

KaroX can call configured stdio or Streamable HTTP MCP servers through its MCP
client. An external MCP server is executable third-party code or a remote
service and must be treated as untrusted.

Requirements:

- explicit server registration;
- explicit session selection;
- per-tool allow/ask/deny decision;
- credential values stored only in the MCP keyring namespace;
- schema and identity binding;
- no command, environment, credential, or internal URL reflected to hosted
  client descriptors;
- mutating retries only under the idempotency contract.

A selected `process.run` or MCP capability is powerful and is not an OS sandbox.

## Connection status vocabulary

- **Contract tested** means local deterministic end-to-end protocol coverage.
- **Live tested** means a dated real external product/provider run exists in
  `docs/conformance/`.
- **Experimental** means the live product contract is incomplete or unstable.
- **Legacy** means retained for compatibility while a replacement is validated.

The current release status is summarized in `docs/IMPLEMENTATION_STATUS.md` and
the full release gates live in `docs/V5_RELEASE_SCOPE.md`.

## Diagnostics

Start with:

```bash
karox --version
karox paths --json
karox doctor
```

For bridge problems, keep the bridge process open and preserve the exact error
text. Do not share the MCP credential, approval password, access token, refresh
token, API key, or private repository payload.

See [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) and
[SECURITY.md](../SECURITY.md).
