# KaroX 6

A small, fast coding-agent CLI: one agent core shared by a headless CLI (JSON stream), a
light TUI and an MCP server, with API providers for OpenAI-compatible, Anthropic and Gemini
models. Runs on Windows, Linux and macOS. [По-русски](README.ru.md)

Status: under construction — see [PROGRESS.md](PROGRESS.md) and [DECISIONS.md](DECISIONS.md).

## Use

```bash
karox chat --model MODEL            # interactive chat (also: bare `karox` in a terminal)
karox run --model MODEL "task"      # one task headless; --json for a JSON-lines event stream
karox mcp --model MODEL             # serve KaroX to MCP clients over stdio (--http PORT for HTTP)
karox update                        # install the newest karox6/main build here (/update in the chat)
```

No working KaroX yet: `scripts/install_latest.ps1` (Windows) or `scripts/install_latest.sh`
creates a venv and installs the newest build into it.

Provider flags: `--provider openai-chat|openai-responses|anthropic|gemini`, `--base-url`,
`--api-key-env`. External MCP servers become tools when listed in `~/.karox6/mcp.json`
(or `--mcp-config`) under `mcpServers` (`command`/`args` or `url`).
`pip install karox6[tui]` adds line editing and history to the chat.

Defaults for these flags live in `~/.karox6/config.toml` (or `$KAROX_HOME/config.toml`);
flags and `KAROX_MODEL` / `KAROX_PROVIDER` / `KAROX_BASE_URL` override it, and
`--profile NAME` overlays a `[profiles.NAME]` table:

```toml
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"   # the variable's name, never the key itself

[profiles.local]
model = "qwen3-coder"
base_url = "http://127.0.0.1:11434/v1"
```

Full user guide (chat, headless runs and exit codes, limits, safety, MCP both ways):
[docs/guide.md](docs/guide.md). Using KaroX from ChatGPT, Claude and other web clients (Tailscale tunnel built in): [docs/chatgpt.md](docs/chatgpt.md).

## Development

```bash
uv sync
uv run pytest
uv run karox --version
```

License: MIT. Third-party material: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
