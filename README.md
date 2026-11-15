# KaroX 6

A small, fast coding-agent CLI: one agent core shared by a headless CLI (JSON stream), a
light TUI and an MCP server, with API providers for OpenAI-compatible, Anthropic and Gemini
models. Runs on Windows, Linux and macOS.

Status: under construction — see [PROGRESS.md](PROGRESS.md) and [DECISIONS.md](DECISIONS.md).

## Development

```bash
uv sync
uv run pytest
uv run karox --version
```

License: MIT. Third-party material: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
