# KaroX vNext connectivity

KaroX is the local runtime and policy boundary. It supports three distinct
connection directions; they must not be conflated.

## 1. CLI to model-provider APIs

The native agent calls configured OpenAI Responses, OpenAI-compatible Chat
Completions, Anthropic Messages, or Gemini Generate Content APIs. Provider
credentials remain in the OS keyring; model tool calls return to KaroX and all
local actions execute through Core.

```powershell
karox credential set openai
karox provider add openai `
  --adapter openai_responses `
  --base-url https://api.openai.com/v1 `
  --credential-ref os-keyring:provider/openai
karox model add openai MODEL_ID --alias default --tools true --streaming true
karox model select openai default
karox provider test openai --model default
karox agent run `
  --repository . `
  --session-id native-1 `
  --task "Implement the requested change" `
  --verification-command '["python","-m","unittest","discover","-s","tests"]'
```

Ollama, LM Studio, vLLM, OpenRouter, and another compatible endpoint use the
same flow with `--adapter openai_compatible_chat` and their own base URL.

## 2. Hosted client to KaroX

A hosted client can call an explicit allowlist of built-in KaroX Core tools,
explicitly selected external MCP tools, or both. Nothing is auto-exposed.

Create the session and a one-time bridge credential:

```powershell
karox session create --repository . --task "Hosted task" `
  --access-profile workspace_write --id hosted-1
karox bridge credential set hosted-1 --json
```

### PromptQL/OpenAPI

```powershell
karox bridge serve `
  --profile promptql `
  --protocol openapi `
  --repository . `
  --session-id hosted-1 `
  --credential hosted-1 `
  --tool karox.repo.read_file `
  --tool karox.repo.list_files `
  --tool karox.repo.write_file `
  --tool karox.git.status `
  --tool karox.git.diff
```

PromptQL imports `https://PUBLIC_HOST/openapi.json`. Put the generated bridge
secret only in the connector's protected Bearer or `X-API-Key` field; the schema
itself is authenticated, so the credential has to be stored before the import.
The authenticated endpoints are `/openapi.json`, `/health`, `/session`,
`/context/brief`, and `/tools`. Only `/` is open, and it carries nothing but the
service name and the schema URL. Mutating operations require a caller-generated
stable `X-KaroX-Idempotency-Key` header.

The server binds to loopback by default. A cloud-hosted client needs a separate
HTTPS tunnel or reverse proxy. Do not publish the raw HTTP port directly. A
non-loopback `--host` requires both `--allow-network-bind` and a TLS pair
(`--tls-certfile` with `--tls-keyfile`); KaroX refuses to answer a network
interface in clear text.

### Streamable HTTP MCP

Use the same command with `--protocol mcp`; the endpoint is `/mcp`. An MCP
mutation may carry `_meta.karoxIdempotencyKey`; a client that cannot send `_meta`
gets a key derived from the call, and a replay says so in
`idempotent_replay`. `--server SERVER_ID` additionally
proxies an external MCP server that was already selected and allowed for the
session.

### ChatGPT Web and Claude Web over OAuth MCP

The `chatgpt-web` and `claude-web` bridge profiles implement the remote-MCP
pattern used by [AgentDock](https://github.com/uvwt/agentdock): OAuth protected
resource discovery, authorization-server metadata, Dynamic Client
Registration, Authorization Code with PKCE S256, access tokens, and rotating
refresh tokens. AgentDock is an MCP runtime, not an LLM inference provider, so
these profiles belong to `bridge`, not to the model-provider registry.

The primary path is one CLI command. It creates a repository-bound session and
temporary approval credential, starts a Cloudflare Quick Tunnel, starts the
OAuth MCP bridge with the tunnel's exact HTTPS origin, and supervises both
processes:

```powershell
karox bridge connect chatgpt-web --repository . --write
karox bridge connect claude-web --repository . --write
```

Without `--write`, the command exposes only the safe read-only repository and
Git tool set under a `read_only` session. `--write` adds `edit_file` and
`write_file` and selects `workspace_write`. The command prints the generated
MCP URL and temporary bridge approval password. Add that URL to the web client:

- ChatGPT: create a custom MCP app in developer mode and scan its tools.
- Claude: open **Settings > Connectors > Add custom connector**.

Do **not** paste the approval password into the connector form. The browser
opens KaroX's approval page during OAuth; verify the client and resource, then
enter the password there. Press `Ctrl+C` in the CLI to stop the bridge and
tunnel and remove the temporary credential. Only the safe defaults, tools
named with `--tool`, and the two write tools requested by `--write` are
exposed.

If the launcher is killed hard instead (`taskkill /F`, a closed console), the
children are killed with it by a Windows job object or, on Linux, by
`PR_SET_PDEATHSIG`. macOS has no equivalent primitive, so there a tunnel can
outlive its launcher until `karox bridge doctor` — or the next `bridge connect` —
signals the recorded process group, revokes the session and credential, and
reports what it reaped.

`cloudflared` is installed by the Windows KaroX installer when accepted, or can
be selected explicitly with `--cloudflared PATH`. For an existing stable
reverse proxy, keep the lifecycle in the CLI but replace the managed Quick
Tunnel:

```powershell
karox bridge connect chatgpt-web --repository . --write `
  --tunnel custom --public-url https://device.example.com
```

The lower-level `session create`, `bridge credential set`, and `bridge serve`
commands remain available for automation that intentionally manages each
resource separately; they are not required for the normal web connection.

Registered OAuth clients and refresh grants survive a restart. They are kept in
`<runtime dir>/vnext/oauth-bridge`, one file per resource, mode 0600, holding
SHA-256 digests rather than tokens — enough to recognise a token, never enough to
be one. The state is bound to the exact resource URL that issued it, so a bridge
that comes back on a different origin starts empty rather than honouring a grant
minted elsewhere. Authorization codes and half-finished approvals are not
restored: they live for minutes and belong to a browser flow the restart already
interrupted. A replayed refresh token still revokes its whole token family, and
that revocation is persisted too.

That makes the connector durable only if its URL is. A Quick Tunnel receives a
new public URL on every run — the exact URL is bound before the bridge starts,
but a restart moves it, and the connector then points at a host that no longer
exists. Use `--tunnel custom` with a stable HTTPS origin for a connector that is
meant to keep working; `bridge connect` prints this warning itself when the
profile needs a stable URL and none was given.

The wire contract is tested locally, but no live ChatGPT or Claude account run is
claimed yet; both profiles therefore remain `experimental`.

## 3. CLI to a hosted web agent

A hosted agent that exposes a documented invocation API can be called from the
CLI or TUI. KaroX implements this direction for PromptQL's Natural Language API;
the contract was verified against the official `hasura/promptql-python-sdk`
source (`client.py`) and is exercised against a mocked HTTP transport in the
test suite. No live PromptQL run has been recorded yet, so the target status
remains experimental, not `tested`.

PromptQL's Natural Language API is a hosted agent: it executes actions
server-side against its own DDN data sources and returns `assistant_actions`
(`message`, `plan`, `code`, `code_output`, `code_error`), not tool-calls for
local execution. KaroX therefore calls it through a dedicated `target ask`
command, not as a provider in the tool-calling routing loop — mixing the two
would be a semantic mismatch.

Contract (v2, non-streaming):

- `POST {api_base_url}/query` (base URL must not include `/query`).
- `Authorization: Bearer {api_key}`.
- Default base URL: `https://api.promptql.pro.hasura.io`.
- Body: `{"ddn": {"build_version": ...} | {"build_id": ...}, "interactions": [{"user_message": ...}], "stream": false, "timezone": ...}`.
- Response: `{"assistant_actions": [...], "modified_artifacts": [...]}`.

Configure the target, store the API key in the OS keyring, and invoke:

```powershell
karox credential set promptql
karox target add promptql
karox target configure promptql `
  --setting build_version=BUILD_VERSION `
  --setting api_base_url=https://api.promptql.pro.hasura.io `
  --credential-ref os-keyring:provider/promptql
karox target ask promptql --message "Summarize sales by region" --json
```

The API key is resolved from the OS keyring at request time, never written to
configuration, and redacted from results, errors, and logs. Streaming
(`stream: true`) and v1 (`ddn_url` with explicit LLM config) modes are deferred
with explicit errors, not half-built. Inside the interactive shell, `/ask TEXT`
(or `/ask --target promptql TEXT`) runs the same call and renders the
assistant messages in the conversation.

A hosted agent whose protocol differs from the documented Natural Language API
needs a small audited adapter; do not assume OpenAI-compatibility for an
undocumented contract.

## Interactive shell

Run `karox` without arguments to open the full-screen terminal client in the
current repository. The first launch asks only for Russian or English, then
opens the chat immediately. Normal text is an agent task, not raw command-line
syntax. Type `/` for the keyboard command menu and run `/connect` to choose API,
site, or both. API setup can discover models and
advertised token limits with `F5`; the user reviews or edits them, then `F10`
performs a real minimal request before activation. `Ctrl+B` directly opens the
ChatGPT Web, Claude Web, PromptQL, Notion, or generic MCP/OpenAPI bridge screen.
The `/connect` website choice names ChatGPT and Claude explicitly and their
profiles invoke the same managed `bridge connect` lifecycle. When
`cloudflared` is installed, it creates the public HTTPS connector URL
automatically. The explicit scriptable equivalent remains
`karox tui --repository .`; `karox-vnext` is only a compatibility alias.
