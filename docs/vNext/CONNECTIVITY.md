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
secret only in the connector's protected Bearer or `X-API-Key` field. The
authenticated preflight endpoints are `/health`, `/session`, `/context/brief`,
and `/tools`. Mutating operations require a caller-generated stable
`X-KaroX-Idempotency-Key` header.

The server binds to loopback by default. A cloud-hosted client needs a separate
HTTPS tunnel or reverse proxy. Do not publish the raw HTTP port directly.

### Streamable HTTP MCP

Use the same command with `--protocol mcp`; the endpoint is `/mcp`. MCP
mutations require `_meta.karoxIdempotencyKey`. `--server SERVER_ID` additionally
proxies an external MCP server that was already selected and allowed for the
session.

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
PromptQL, Notion, or generic MCP/OpenAPI bridge screen. When `cloudflared` is
installed, it can create the public HTTPS connector URL automatically. The
explicit scriptable equivalent remains `karox tui --repository .`; `karox-vnext`
is only a compatibility alias.
