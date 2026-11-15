# KaroX 6 user guide

[По-русски](guide.ru.md)

KaroX is a coding agent for the terminal. It reads, searches and edits files and runs shell
commands in your project until the task is done and checked. One agent core serves three
front ends: an interactive chat, a headless runner for scripts and CI, and an MCP server for
other AI clients.

## Install

KaroX needs Python 3.12 or newer and no compilers.

```bash
uv tool install karox6            # or: pipx install karox6
uv tool install "karox6[tui]"     # adds line editing and input history to the chat
karox --version
```

Until the first release is on PyPI, install the wheel instead:
`uv tool install "dist/karox6-<version>-py3-none-any.whl[tui]"` (build it with `uv build --wheel`).

## Connect a model

KaroX talks to any OpenAI-compatible endpoint (`openai-chat`, the default), the OpenAI
Responses API (`openai-responses`), Anthropic (`anthropic`) and Gemini (`gemini`). The API
key is read from an environment variable whose *name* you give; keys are never accepted as
command-line arguments, and KaroX removes the variable from its own environment after
reading it, so commands the agent runs cannot print it.

```bash
export DEEPSEEK_API_KEY=...        # once, in your shell profile or a secret store
karox chat -m deepseek-chat --base-url https://api.deepseek.com/v1 --api-key-env DEEPSEEK_API_KEY
```

Put the defaults in `~/.karox6/config.toml` (or `$KAROX_HOME/config.toml`) so the command
line stays short. Command-line flags beat `KAROX_MODEL` / `KAROX_PROVIDER` /
`KAROX_BASE_URL`, which beat the file; `--profile NAME` overlays a `[profiles.NAME]` table:

```toml
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"   # the variable's name, never the key itself

[profiles.local]
model = "qwen3-coder"
base_url = "http://127.0.0.1:11434/v1"
api_key_env = ""
```

Other useful settings: `reasoning_effort`, `temperature`, `context_window`,
`max_output_tokens`, prices (`price_input`, `price_output`, `price_cached`, USD per 1M
tokens; they enable cost display and `--max-cost`).

## Chat

`karox chat` (or plain `karox` in a terminal) opens a chat in the normal scrollback: the
answer streams in, each tool call is one line, and a destructive command asks before it
runs (`y` / `N` / `a` = allow for the rest of the session).

| Command | Does |
|---|---|
| `/diff` | workspace changes: `git diff` plus untracked files |
| `/status` | session, model, tokens and cost |
| `/new` | start a fresh session |
| `/sessions`, `/resume [ID\|last]` | list and continue saved sessions |
| `/approve ask\|allow\|deny` | how destructive commands are handled |
| `/reasoning` | show or hide the model's reasoning |
| `/help`, `/quit` | help; leave (Ctrl+D works too) |

Ctrl+C stops the running turn and keeps the session; end a line with `\` to type more lines.

## Headless runs

```bash
karox run "fix the failing test in tests/test_api.py"
echo "add type hints to utils.py" | karox run -
karox run --json "..." > events.jsonl      # one JSON event per line
karox run --resume last "now update the changelog"
```

Exit codes: `0` completed, `1` error, `2` bad arguments, `3` a limit was hit, `4` the
agent was stopped for repeating itself, `130` cancelled.

With `--json` every line is an event with a `type`: `run_started`, `assistant_message`,
`tool_started`, `tool_finished`, `approval_requested`, `approval_resolved`, `usage`,
`retry`, `notice`, `compacted`, `error`, `run_finished` (`--stream` adds `text_delta` and
`reasoning_delta`). `run_finished` carries the final status and reason.

Limits:

- `--max-steps N`: model calls per turn. Default 200 for `run` and `mcp`, none for `chat`;
  `0` means no limit.
- `--wrap-up-steps N`: after N steps (default 120) the agent is told to finish its current
  line of work and verify; 30 steps later it is asked for its final answer. `0` turns this
  off.
- `--max-seconds`, `--max-cost` (needs prices).

## Safety

KaroX works on its own for ordinary engineering work: reading, editing, tests, builds and
local git commands run without prompts. It does not commit, push or create branches unless
the task asks for it. A small set of destructive shell commands is held back: recursive
deletes of the root, home, the workspace itself or paths outside it, deleting `.git`,
writes to block devices, disk formatting and erasing, `git push --force` and its variants,
and `DROP DATABASE` through database clients. The check looks inside `bash -c`, `$(...)`,
pipes into shells and PowerShell equivalents.

`--approve-dangerous` chooses what happens to those commands: `ask` (default in a terminal),
`deny` (default without a terminal) or `allow`.

Sessions are saved as JSON lines under `~/.karox6/sessions/<id>/session.jsonl`; at the end of
a run the files it changed are also copied to `<session>/submissions` (`--no-snapshot` turns
this off).

## External MCP servers as tools

List servers in `~/.karox6/mcp.json` (or pass `--mcp-config FILE`; `--no-mcp` skips them):

```json
{
  "mcpServers": {
    "docs": {"command": "uvx", "args": ["some-mcp-server"], "env": {"LOG_LEVEL": "warn"}},
    "tracker": {"url": "https://example.com/mcp", "headers": {"X-Team": "core"},
                "bearer_token_env": "TRACKER_TOKEN"}
  }
}
```

Tokens come from the environment variable named in `bearer_token_env`, never from the file.
Their tools appear to the agent as `mcp__<server>__<tool>`. Tools marked read-only by the
server run in parallel with other reads; large outputs are cut and the rest saved to a file the
agent can read. A server that fails to start is skipped with a warning.

## KaroX as an MCP server

`karox mcp` serves KaroX over stdio; `karox mcp --http 8765` serves Streamable HTTP on
`127.0.0.1` with a bearer token. Clients get seven tools: `karox_run` (start a task;
idempotent with `request_id`), `karox_status` (long-poll with `wait_seconds`; a short
one-line-per-action `log`), `karox_approve`, `karox_cancel`, `karox_diff`, `karox_files`,
`karox_runs`. Runs stay inside `--root` unless `--any-cwd` is given.

Example for a client that starts servers itself (Claude Desktop, Cursor and similar):

```json
{"mcpServers": {"karox": {"command": "karox", "args": ["mcp", "--root", "/path/to/project"]}}}
```

Using KaroX from ChatGPT on the web: [chatgpt.md](chatgpt.md).

## Troubleshooting

- `--model is required`: pass `-m` or set `model` in `config.toml`.
- 429 and provider outages (502/503/529) are retried with backoff; `retry` events show it.
- On Windows `python3` is often only the Microsoft Store placeholder; KaroX tells the agent
  which interpreter really works.
- `karox debug rss` loads the headless stack and prints its resident memory.
