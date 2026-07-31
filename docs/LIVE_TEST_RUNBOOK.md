# KaroX 5 live conformance runbook

Status: required before stable `5.0.0`  
Last source review: 2026-07-29

This runbook turns third-party account tests into repeatable release evidence.
Provider and client interfaces can change without a KaroX commit, so verify the
official product documentation immediately before each release-candidate run.

Official references reviewed for this runbook:

- OpenAI: `https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta`
- Anthropic: `https://support.anthropic.com/en/articles/11175166-about-custom-integrations-using-remote-mcp`

Do not record credentials, private source, personal data, or unredacted HTTP
captures.

## Shared prerequisites

Before every run:

1. Use the exact release-candidate commit.
2. Record `karox --version`, commit SHA, OS, and Python version.
3. Use a disposable Git repository with one deterministic test command.
4. Confirm the repository contains no real credential.
5. Start with Observe and inspect the repository before switching to Build.
6. Decide whether the run uses a Quick Tunnel or a stable HTTPS origin.
7. Keep the KaroX bridge process open for the complete hosted-client run.
8. Prepare a sanitized evidence destination outside the repository under test.

## ChatGPT Web

### Current product requirements

OpenAI currently documents developer mode and custom MCP apps on ChatGPT web.
Full MCP write/modify support is rolling out for Business, Enterprise, and Edu.
Developer-mode availability and who may create an app depend on workspace role
and plan. Pro may have more limited custom-app/MCP behavior.

Do not tell a user to open **Plugins**. The current UI is under **Apps**.

### Enable developer mode

Depending on workspace and role, the current official paths include:

- Workspace Settings → Permissions & Roles → Connected Data developer mode /
  Create custom MCP connectors;
- Workspace Settings → Apps → Create;
- Settings → Apps → Advanced Settings;
- Settings → Apps → Create for an authorized developer.

Record which path actually appeared. A missing control is an entitlement or
workspace-policy result, not an OAuth defect in KaroX. KaroX should diagnose the
requirement instead of sending the user through nonexistent menus.

### Create the app

1. Start the KaroX bridge:

   ```bash
   karox bridge connect chatgpt-web --repository . --write
   ```

2. Copy only the printed MCP URL.
3. In ChatGPT Apps, create a custom app and provide the MCP endpoint and required
   metadata.
4. Select the applicable OAuth authentication option.
5. Run **Scan Tools**.
6. Complete the browser authorization prompt.
7. On the KaroX approval page, verify the client/resource and enter the printed
   OAuth approval password.
8. Complete tool scanning and create/save the app.
9. Start a new chat and select the draft/dev app.

### Required scenario

- initialize MCP;
- verify that only explicitly selected tools are visible;
- read one file;
- make one bounded file mutation;
- repeat the same mutation with the same idempotency identity and confirm no
  duplicate effect;
- run the approved deterministic check;
- inspect Git status and diff;
- receive the evidence-backed report;
- exercise access-token expiry/refresh where practical;
- confirm refresh rotation and replay-family revocation through the automated
  contract plus any observable live behavior;
- restart KaroX;
- record Quick Tunnel or stable-origin behavior;
- record whether the app/tool snapshot must be refreshed after schema changes.

### Pass criteria

`docs/conformance/chatgpt-web.md` may be changed to `status: passed` only when the
complete primary scenario succeeds on a real account and a sanitized evidence
reference is present.

## Claude Web

### Current product requirements

Anthropic currently documents custom remote MCP connectors for Pro, Max, Team,
and Enterprise flows. Organization-managed plans may have an organization
connector view; individual Pro/Max flows use the Connectors section directly.

### Add the connector

1. Start the KaroX bridge:

   ```bash
   karox bridge connect claude-web --repository . --write
   ```

2. Open Settings → Connectors.
3. For an organization-managed account, select Organization connectors when
   required.
4. Click **Add custom connector**.
5. Paste only the printed remote MCP URL.
6. Add the connector and complete OAuth through KaroX.
7. Enter the approval password only on the KaroX approval page.
8. Enable the connector from the chat's Search and tools control when required.

### Required scenario

Use the same read → mutate → idempotent replay → check → Git status/diff →
evidence → restart sequence as ChatGPT. Record connector persistence, OAuth
refresh behavior, and any tier/organization controls that affect the result.

### Pass criteria

`docs/conformance/claude-web.md` may be changed to `status: passed` only after the
complete real-account scenario and sanitized evidence exist.

## Provider live runs

Run each provider in a disposable repository using a minimal tool-call scenario.
The goal is protocol conformance, not a benchmark or a large coding task.

Required flow:

1. Store the credential through the OS-keyring path.
2. Configure the exact official endpoint and model ID.
3. Send a minimal request that causes one read-only Core tool call.
4. Return the tool result to the provider.
5. Receive a final provider response.
6. Repeat with one bounded mutation and approved verification when the adapter's
   live tool contract is proven safe.
7. Record authoritative usage where the provider returns it.
8. Confirm the secret is absent from config, stdout, stderr, audit, session,
   evidence, and support diagnostics.
9. Record timeout, rate-limit, malformed-response, and unsupported-capability
   behavior without intentionally spending a large amount.

Required records:

- `docs/conformance/openai-responses.md`;
- `docs/conformance/anthropic-messages.md`;
- `docs/conformance/gemini.md`;
- `docs/conformance/openai-compatible.md`.

A generic compatible run proves only the named endpoint/model combination. It
must not become a claim that every OpenAI-compatible service works.

## Sanitized evidence format

A live record should include:

- `status: passed`;
- ISO 8601 UTC time;
- KaroX version and commit;
- OS and Python;
- account tier/role or API project type;
- visible external product/model version;
- exact scenario and result;
- limitations;
- evidence reference.

The evidence reference may point to:

- a redacted CI artifact;
- a sanitized local report checked into a dedicated evidence repository;
- a screenshot with all private identifiers removed;
- a structured KaroX evidence summary with repository content excluded.

Never use a raw browser HAR, OAuth database, keyring dump, environment dump, or
complete private repository archive as evidence.

## Failure handling

A failed live attempt is useful evidence. Set `status: failed`, record the exact
sanitized failure stage, and classify it as:

- KaroX protocol/runtime defect;
- stale user instruction;
- missing account entitlement or workspace role;
- third-party outage/change;
- local tunnel/network problem;
- test-environment problem.

Do not weaken redirect, resource, origin, capability, idempotency, push, publish,
or path protections merely to turn a failed live run green.
