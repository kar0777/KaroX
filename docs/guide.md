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

### Staying up to date

`karox6 update` installs the newest build of the `karox6/main` branch into the environment
KaroX runs from: it clones the branch into a temporary folder, says what changes
(`6.0.0.dev34 -> 6.0.0.dev40` and the commit titles in between), builds the wheel, checks
that it starts, and only then replaces the installed one. If anything fails, the installed
KaroX stays as it was. `karox6 update --check` only looks; `--force` reinstalls.
`/update` in the chat does the same (restart KaroX afterwards). On Windows the running
`karox6.exe` is renamed out of the way, so the update works from inside the chat too.

The chat looks for a newer version at most once a day, in the background, and says so in
one line (`version X is available: /update`); it never installs anything by itself.
`update_check = false` in `config.toml` (or `KAROX_UPDATE_CHECK=0`) turns that off;
`update_repo` / `update_branch` point it elsewhere.

Without a working KaroX, `scripts/install_latest.ps1` (Windows) or `scripts/install_latest.sh`
creates the venv if needed and installs or updates KaroX in it; run again, it says
"up to date". `-Venv` / `--venv` chooses the folder (default `~/karox6-venv`).

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

`context_window` is what the model accepts; `context_limit` (default 400000) is your
working budget. History is compacted near the smaller of the two, so a 1M-token model
still works in 400k unless you raise the limit.

## Chat

`karox chat` (or plain `karox` in a terminal) opens a full-screen chat: the conversation
scrolls above; below it a line says what the running turn does and for how long (`esc`
stops it); the input box shows the keys that matter right now in its lower edge; the
status bar shows the model, the project and branch, the context fill, the cost (when
prices are set), the last turn's time and the approval mode. The answer streams in as
formatted text (headings, lists, `code`, code blocks with colours, aligned tables), each
tool call is one line (an edit also shows the lines it changed, a command how many lines
it printed) and **Ctrl+O** unfolds or folds the full output of every call. A destructive
command asks before it runs (`y` / `N` / `a` = allow for the rest of the session).
`--inline` keeps the chat in the terminal's normal scrollback instead.

Colours follow the terminal: true colour in Windows Terminal, VS Code, iTerm2, kitty and
WezTerm, 256 or 16 colours elsewhere, none with `NO_COLOR` or `--no-color`. `theme` in
`config.toml` (or `--theme`, `KAROX_THEME`) picks `dark` or `light` (tuned, with tinted
diff lines), `mid` (readable on any background; the default when the terminal does not say
which it has) or `ansi` (the terminal's own 16 colours). In the classic Windows console
window the symbols are limited to what its fonts draw.

| Command | Does |
|---|---|
| `/diff` | workspace changes: `git diff` plus untracked files |
| `/undo`, `/redo` | take the last turn back (its messages and the files it changed), or put it back |
| `/fork` | continue in a copy of this conversation; the original stays as it is |
| `/init [FOCUS]` | have the model write `AGENTS.md` for this project |
| `/paste` (Alt+V) | attach the picture in the clipboard to the next message |
| `/mcp [SERVER]` | MCP servers: state, tools, how to add one |
| `/status`, `/cost` | session, model, tokens, context fill and cost |
| `/compact` | shrink the conversation to free context |
| `/new`, `/clear` | start a fresh session; clear the screen |
| `/sessions`, `/resume [ID\|last]` | list and continue saved sessions |
| `/model [NAME]`, `/setup`, `/key` | change the model, the provider, the API key |
| `/approve ask\|allow\|deny` | how destructive commands are handled |
| `/reasoning` | show or hide the model's reasoning |
| `/update [check\|force]` | install the newest KaroX (see "Staying up to date") |
| `/help`, `/quit` | help; leave (Ctrl+D works too) |

Esc or Ctrl+C stops the running turn and keeps the session. Enter sends; Alt+Enter, Ctrl+J
or a trailing `\` start a new line. `@path` attaches a file, `@shot.png` (or a dragged
picture's path) attaches a picture. Tab completes commands and paths, Ctrl+R searches the
input history, PgUp/PgDn and the mouse wheel scroll. A message sent while the agent works
waits its turn.

`/undo` records the project around each turn in a private git store under
`<home>/snapshots`; your own repository, index and stash are never touched, ignored files
are not recorded, and a file you changed after the turn is left alone. `--no-undo` switches
the recording off. `karox chat -c` continues the project's last session, `--fork ID|last`
starts a new one from a copy of it.

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

- `--max-steps N`: model calls per turn (no limit by default).
- `--wrap-up-steps N`: after N steps the agent is told to finish its current line of work
  and verify; 30 steps later it is asked for its final answer (off by default).
- `--max-seconds`, `--max-cost` (needs prices).

## Safety

KaroX works on its own for ordinary engineering work: reading, editing, tests, builds and
local git commands run without prompts. A small set of destructive shell commands is held back: recursive
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
                "bearer_token_env": "TRACKER_TOKEN", "timeout": 120}
  }
}
```

Tokens come from the environment variable named in `bearer_token_env`, never from the file;
`${VAR}` (or `${env:VAR}`) in `env`, `args`, `headers` and `url` is taken from the
environment too, so configs written for other MCP clients work as they are. Pictures a
server returns (a browser screenshot) are saved to a file named in the tool's output.
Their tools appear to the agent as `mcp__<server>__<tool>`. Tools marked read-only by the
server run in parallel with other reads; large outputs are cut and the rest saved to a file the
agent can read. `timeout` (seconds, default 600) limits each call; a call that runs over it,
or that you interrupt, is cancelled on the server too. A server that fails to start is skipped
with a warning that quotes the end of its stderr. A local server that crashes mid-run is
restarted on its next call (up to 3 times); when a remote server forgets the session (a
restart, an idle expiry), KaroX opens a new one and repeats the call once.

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

Claude Code: `claude mcp add karox -- karox mcp --root /path/to/project`. Codex CLI, in
`~/.codex/config.toml`:

```toml
[mcp_servers.karox]
command = "karox"
args = ["mcp", "--root", "/path/to/project"]
```

The model, provider and key come from your KaroX settings and environment as usual (or add
`--model ...` to the arguments). When the client goes away (it closes the connection, stops
the server with SIGTERM, or kills it outright on Windows), running tasks are cancelled and
their commands stopped; a request the client cancels gets no answer, while its run goes on
until `karox_cancel`.

Using KaroX from ChatGPT, Claude and other web clients (Tailscale tunnel built in): [chatgpt.md](chatgpt.md).

## Experimental options

All are off by default and stay so until they win an A/B run on the benchmark.

- `--copy-notes`: after an `edit`, KaroX looks for other places in the project (files of the
  same type, tracked by git when it is a repository) that still hold three consecutive lines
  of the code the edit replaced, and appends up to five of them to the edit result, each
  place once per run. A fix made in one copy of duplicated code is then visible in the
  others. The search is limited to 1.5 s and never fails the edit.

- `--code-mode` adds `run_code`: the agent writes one Python program that calls the other
  tools as functions (`tools.bash(command=...)`, `tools.read(file_path=...)`, ...) and gets
  back only what the program prints, so several tool calls cost one step. Nested calls go
  through the same checks and approvals as direct ones; a failing call raises `ToolError`
  and, unless caught, fails the program. Programs are kept in `<session>/code/`.
- `--subagents` adds `agent`: the agent hands an investigation to a child session with a
  fresh context (read, search and shell only) and gets back only its report.

## Troubleshooting

- `--model is required`: pass `-m` or set `model` in `config.toml`.
- 429 and provider outages (502/503/529) are retried with backoff; `retry` events show it.
- On Windows `python3` is often only the Microsoft Store placeholder; KaroX tells the agent
  which interpreter really works.
- `karox debug rss` loads the headless stack and prints its resident memory.
