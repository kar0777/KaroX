# Terminal client

`karox` or `karox tui` opens the terminal client. It reads like a CLI session:
one transcript that only grows, a composer at the bottom and one status line
under it. There are no tabs, no function-key screens and no side panels. A line
that starts with `/` is a command; typing `/` opens the command list right
above the composer, and every picker (model, session, provider, MCP server,
skill, effort, mode, language) opens in the same place and is filtered by
typing. Command output — `/help`, `/status`, `/session`, `/doctor`, a skill's
detail, the last reasoning — is printed into the transcript like any CLI
prints it.

| File | Role |
| --- | --- |
| `tui/app.py` | application, layout, slash-command dispatch, run state, background CLI calls |
| `tui/commands.py` | the one table of slash commands (palette, `/help` and dispatcher share it) |
| `tui/views.py` | pure builders: help/status/session/doctor text, picker rows |
| `tui/widgets.py` | transcript lines, tool line, inline menu, composer, key prompt, status line |
| `tui/modals.py` | the two multi-field forms: custom endpoint and MCP server |
| `tui/backend.py` | subprocess bridge and the `--json` CLI calls |
| `tui/protocol.py` | versioned event/report decoding, tool categories, session-state parsing |
| `tui/i18n.py` | English/Russian message catalogue |
| `tui/prefs.py` | the `vnext/ui.json` slice the client owns |
| `tui/theme.tcss` | palette and layout |

## Layout

```text
◆ KaroX
  proj · main
  wandb/deepseek-ai/DeepSeek-V3.1
  session tui-1762190000-3fa2c1
  detected checks: python -m pytest -q · enable with /verify <command>

› fix the failing test in calc.py
  ◇ reasoning · 1,234 chars
  I'll read calc.py first.
  ✓ repo.read_file  0.05s  read 40 lines
  ✎ repo.edit_file  running…
  ✓ checks.run  2.31s  exit 0
  Fixed the sign in add(); the suite passes.
✓ done · verified · 4 steps · files: 1 · checks: 1/1 · tokens: 18.2k · cost $0.0031 · 35.3s
  changed: calc.py

› /model
  ┌ Model ─────────────────────────────────────────────────────┐   ← inline list,
  ● wandb/deepseek-ai/DeepSeek-V3.1  current · ctx 128.0k · $0.55/$1.65 per 1M
    wandb/Qwen/Qwen3-Coder-480B      ctx 262.1k · $1/$1.5 per 1M
  ↑↓ choose · Enter confirm · Esc cancel
╭──────────────────────────────────────────────────────────────╮
│ Describe a task…   / commands · Ctrl+J newline               │
╰──────────────────────────────────────────────────────────────╯
  wandb/deepseek-ai/DeepSeek-V3.1 · 18.2k tok · proj        idle · saved 11-03 14:22
```

- The answer of one turn is a sequence of text stretches interleaved with tool
  lines in the order they happened: a new model step, a tool call or a context
  compaction closes the current stretch, so text written after a tool appears
  after it instead of being glued to the text before it. The report's final
  message is shown only when nothing streamed.
- A tool line runs behind its category icon (`○` read, `✎` edit, `≡` test,
  `⬢` MCP, `★` skill, `▤` plan, `◎` web, `⊙` inspect) and switches to `✓`/`✗`
  on finish. A click expands it in place with its unclipped observation; a
  second click folds it.
- A finished run adds one compact result line: outcome, steps, files, checks,
  tokens, cost (when the model is priced), elapsed time; the reason, unmet
  requirements, the failure map and the changed files follow on their own
  lines only when there are any.
- The status line keeps the model as the primary fact at every width; tokens,
  the context note (only near the window limit), mode and effort (only when
  not the default), repository and branch follow and drop whole, least
  important first, when they no longer fit. The run state sits on the right:
  `idle · saved <time>`, `working · step 3 · 12s · ✓1/✗0`, `stopping…`.

## Keys

| Key | Action |
| --- | --- |
| Enter | send the task; in a list, run or choose the highlighted row |
| `/` | open the command list (typing filters it) |
| Tab | in the command list, complete the command and keep typing its argument |
| ↑ / ↓ | move in a list; otherwise recall submitted lines (at the document edges) |
| Esc | close the list or the key prompt; otherwise stop the running agent |
| Ctrl+J | insert a newline |
| PageUp / PageDown | scroll the transcript (move by a page in a list) |
| Ctrl+N | new session |
| Ctrl+R | print the last answer's reasoning |
| Ctrl+L | clear the screen |
| Ctrl+C | close what is open, else stop the agent, else clear the draft, else exit |
| Ctrl+Q | exit |

The keyboard never leaves the composer: while a list is open, the composer
forwards ↑/↓, PageUp/PageDown, Tab, Enter and Esc to it and what you type
filters it. Enter in a picker never sends its filter text as a task. A
recalled `/command` does not pop the list open, so ↑ keeps walking the
history. The mouse scrolls the transcript, clicks list rows and expands tool
lines.

## Slash commands

```text
/help                  commands and keys                         (/?)
/connect [provider]    connect a provider with its API key       (/provider, /login)
/model [provider/model] choose a model; with an argument, select it directly
/sessions [id]         this repository's sessions; resume one    (/resume)
/new                   new session
/session               the saved session state
/status                model, session, checks, counters
/mcp [add]             MCP servers: probe, allow tools, remove, add
/skills                repository skills; Enter prints one
/verify <command>      add a verification command for this window
/unverify              clear the verification list
/effort [level]        auto | low | medium | high | extra-high | ultra
/mode [mode]           build | plan | ideate
/lang [ru|en]          interface language
/reasoning             the model's reasoning for the last answer
/doctor                provider, credential, MCP and session health
/clear                 clear the screen
/stop                  stop the agent
/quit                  exit                                      (/exit, /q)
```

A command with an optional argument opens its picker when it is given none.
The table lives in `tui/commands.py`; the palette, `/help` and the dispatcher
read the same rows, and a test pins that every row has a handler and both
translations.

## Connecting a provider

`/connect` lists the bundled models.dev catalog (`karox provider catalog
--json`): connected providers first, each row with its model count and the
environment variable its key usually lives in. Choosing one swaps the composer
for a masked one-line key prompt; the question stays on the line above it.
Enter connects, Esc cancels. The key goes to `karox provider connect ID --json
--stdin-key` on stdin — never into argv, the transcript, the composer history
or the screen. For a provider that is already connected an empty Enter keeps
the saved credential. Endpoints with placeholders (Cloudflare account ID,
Databricks host, …) are asked next in plain text, pre-filled from the
environment when the variable is set, and passed as `--var NAME=VALUE`.

`provider connect` installs every tool-capable model with its published
price and limits and tests the key live. When no other model was active the
new provider's recommended model becomes the selection; otherwise the model
list opens filtered to the new provider with that model highlighted. The last
row, "Custom OpenAI-compatible endpoint…", opens the one form for a base URL,
model id and key by hand (`provider setup`). `/connect ID` skips the list.

## One run

1. The app picks a session id (or the one passed with `--session-id`).
2. On submit it starts:

   ```text
   python -m karox.cli agent run --repository R --session-id S --task T \
       --stream-json [--continue-task] [--effort-level L] [--mode M] \
       [--verification-command JSON]...
   ```

3. It reads stdout line by line. Each line is one JSON document:
   `{"type": "event", ...}` during the run and
   `{"type": "result", "report": {...}}` at the end. A document may carry
   `"v": <int>`; the client speaks version 1, decodes newer versions
   best-effort and ignores unknown fields and kinds, so a kernel that extends
   the stream never breaks an older screen.
4. Text deltas stream into the open answer stretch behind a caret; tool starts
   add one tool line that is updated when the matching `tool_finished`
   arrives; `test_started`/`test_finished` drive the live check tally;
   `step_finished` usage drives the token counter and the context note;
   `phase_changed`, warnings and file events surface on the activity line;
   errors and compaction add a notice.
5. After the run the client re-reads `session show --json` in the background:
   the durable record is the checkpoint state, and the status line shows when
   it was saved.
6. Esc or Ctrl+C stops the child. The session is durable, so the next task
   with `--continue-task` resumes it.

Every other CLI call (model list, catalog, sessions, skills, MCP, doctor,
connect) runs in a worker thread with its stdin closed, so a slow provider
never freezes the screen and a CLI fallback prompt can never wait on a
keyboard the TUI owns. The client never parses human progress text, so
changing a CLI message cannot break a screen.

## Run and session state

`app.RunState` holds the live facts of the current or latest run — state word
(`idle | running | stopping | done | failed | stopped`), step, phase, elapsed,
tokens, context occupancy, and per-category tool counters (reads, edits,
checks passed/failed, MCP calls, skill loads, inspections, edited paths).
Every field comes from a typed stream event or the final report.

`protocol.SessionState` is the bounded, total parse of the durable session
record printed by `/session`: task and turn history, the model's plan
(todo/doing/done), rollback checkpoints, verification checks and the verified
snapshot step, provider usage and costs, MCP servers selected for the session,
loaded skills and changed files. Sections whose record list is empty say
"none" instead of inventing content.

## Event fields

`tui/protocol.py` reads only fields the runtime already declares safe:

```text
kind, step, text_delta, reasoning_delta, reasoning_summary_delta,
tool, call_id, ok, summary, duration_seconds, reason, status, phase,
path, detail, usage
```

Private reasoning is counted on a dim line, never shown as the answer.
`/reasoning` (Ctrl+R) prints it only because the user explicitly asked.

## MCP

`/mcp` lists configured servers with transport, selection, liveness and the
number of allowed tools. Choosing a server offers three actions: probe the
connection, allow its tools for this session (read-only tools become `allow`,
mutating tools `ask`; exact per-tool decisions stay in `karox mcp session
select`) and remove it, which asks for confirmation in the same list. The last
row — or `/mcp add` — opens the form for a stdio command or a Streamable HTTP
URL with environment, headers, credential reference, OAuth, read-only tools
and timeout.

## Testing

The client is tested through Textual's pilot with a recorded backend:
`tests/test_tui.py` feeds `Event` and `Outcome` objects and asserts on the
transcript widgets, the palette (open, filter, Tab completion, Esc), every
picker, the `/connect` key flow (the key never reaches the transcript or the
history), segment ordering around tool lines, the run-state counters, the
checkpoint refresh, status-line fitting, narrow terminals, a Russian screen
with no English leftovers, error and empty states, and the emitter→decoder
round trip against `cli._stream_json`. No model, public network or real
keyring write is involved. The live check is a separate smoke run documented
in [DEVELOPMENT.md](DEVELOPMENT.md).

## Language

The catalogue is `tui/i18n.py`; keys fall back to English and then to the key
itself, so a missing translation is visible instead of crashing a screen. A
test pins the two catalogues to the same key set. The choice is stored in
`vnext/ui.json` under `language` and is shared with the CLI and the next
start.
