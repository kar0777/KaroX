# Agent targets

Targets are external agent environments that connect to KaroX; they are never
shown as chat models. PromptQL, Notion, letaido, generic OpenAPI/MCP and
Hyperagent profiles are available. ChatGPT Web and Claude Web use the separate
`bridge` profiles `chatgpt-web` and `claude-web`, because they call KaroX over
OAuth remote MCP rather than expose an outbound invocation API. Relevance AI
and Dust are experimental; MindStudio and Browser Use require a documented
contract. `karox bridge connect PROFILE` owns the session, OAuth credential,
local bridge, and HTTPS tunnel lifecycle.

Use `karox target presets|list|add|configure|doctor|remove|handoff`. Target
configuration lives in `targets.json`, separately from model providers.
