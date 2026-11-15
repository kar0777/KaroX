# KaroX 6

A small, fast coding-agent CLI: one agent core shared by a headless CLI (JSON stream), a
light TUI and an MCP server, with API providers for OpenAI-compatible, Anthropic and Gemini
models. Runs on Windows, Linux and macOS.

Status: under construction — see [PROGRESS.md](PROGRESS.md) and [DECISIONS.md](DECISIONS.md).

## Use

```bash
karox chat --model MODEL            # interactive chat (also: bare `karox` in a terminal)
karox run --model MODEL "task"      # one task headless; --json for a JSON-lines event stream
karox mcp --model MODEL             # serve KaroX to MCP clients over stdio (--http PORT for HTTP)
```

Provider flags: `--provider openai-chat|openai-responses|anthropic|gemini`, `--base-url`,
`--api-key-env`. External MCP servers become tools when listed in `~/.karox6/mcp.json`
(or `--mcp-config`) under `mcpServers` (`command`/`args` or `url`).
`pip install karox6[tui]` adds line editing and history to the chat.

## Development

```bash
uv sync
uv run pytest
uv run karox --version
```

License: MIT. Third-party material: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
