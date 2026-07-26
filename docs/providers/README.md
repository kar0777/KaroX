# Model providers

KaroX separates model providers from agent targets, tools, and telemetry. Use
`karox provider presets` to see native presets and `karox provider add-preset`
to create one. Presets never contain credentials.

| Preset | Adapter | Status | Base URL |
| --- | --- | --- | --- |
| routing.run | OpenAI-compatible | stable | user-confirmed |
| OmniaKey | OpenAI-compatible | experimental | user-confirmed |
| APIMaster | OpenAI-compatible | experimental | user-confirmed |
| Chutes | OpenAI-compatible | experimental | documented preset |
| EmpirioLabs | OpenAI-compatible | experimental | verified user endpoint |
| Puter | specialised | documentation required | none assumed |
| Tinfoil | OpenAI-compatible | experimental | user-confirmed |
| Vivgrid | OpenAI-compatible | experimental | user-confirmed |
| Merge Gateway | OpenAI-compatible | experimental | user-confirmed |
| OpenRouter | OpenAI-compatible | stable | documented preset |

`experimental` means the common adapter exists but provider-specific contract
tests or current official documentation are incomplete. It does not mean the
provider is unsafe. Vivgrid live paid tests are intentionally not run.

Model aliases are provider-scoped:

```text
karox models discover --provider <id>
karox models map sol <actual-id> --provider <id>
karox models map fable <actual-id> --provider <id>
karox models map kimi <actual-id> --provider <id>
```

AgentDock, ChatGPT Web, and Claude Web are not model API providers. AgentDock is
a remote MCP runtime; ChatGPT/Claude Web are hosted MCP clients. The equivalent
KaroX connection is exposed through `karox bridge` profiles `chatgpt-web` and
`claude-web`, so these entries never appear in the model selector. Launch the
whole connection from the CLI with `karox bridge connect chatgpt-web` or
`karox bridge connect claude-web`.
