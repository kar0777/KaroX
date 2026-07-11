# Notion connection example

The KaroX connection prompt is generated per session. The important fields are:

```text
name: KaroX
server URL: https://<current-session-host>/mcp
transport: Streamable HTTP
authorization: Bearer token
```

Paste the value copied with `K` into the protected credential field, not into chat.

The agent must call `karox_preflight` before reading or changing the repository. After preflight, send the actual task separately, for example:

```text
Add validation for empty project names, run the relevant tests, review the diff,
and create a KaroX commit only if the checks pass. Never push.
```
