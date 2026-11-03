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
╭───────────────────────────────────────────────────────────────╮
│ ◆ Welcome to KaroX                                            │
│                                                               │
│   /help for help · /model to pick a model · /connect …        │
│                                                               │
│   cwd: ~/proj (main)                                          │
│   model: wandb/deepseek-ai/DeepSeek-V3.1                      │
│   tip: /verify python -m pytest -q to check every change …    │
╰───────────────────────────────────────────────────────────────╯

> fix the failing test in calc.py

● Read(calc.py)
  ⎿  Read 42 lines

● Search(divide( in tests/)
  ⎿  Found 3 matches in 2 files

● The bug is in divide: integer division and no zero check.

● Plan
  ⎿  ☒ Find why the tests fail
     ■ Fix divide in calc.py
     □ Run pytest

● Update(calc.py)
  ⎿  Updated: +3 −1
      11 -     return a // b
      11 +     if b == 0:
      12 +         raise ZeroDivisionError("b must not be zero")
      13 +     return a / b
     … +1 line · ctrl+o to expand

● Tests(tests/test_calc.py)

● ● ● ● ●  Running tests tests/test_calc.py… (2s · 2.1k tokens · esc to interrupt)
  ⎿ Step 2 of 3: Fix divide in calc.py
  › Queued: then add tests for pow
╭───────────────────────────────────────────────────────────────╮
│ > Describe a task…                                            │
╰───────────────────────────────────────────────────────────────╯
  ? for shortcuts                    wandb/deepseek-ai/DeepSeek-V3.1 · 2.1k tok
```

- The transcript reads like Claude Code: every model step and tool call is
  one `●` line in the order it happened. A tool line is `Name(what)` —
  `Read(calc.py)`, `Search(divide( in tests/)`, `Run(make lint)`,
  `github · list_issues (MCP)(repo: …)` — with the result hung under it on a
  `⎿` line in plain words ("Read 42 lines", "Tests passed · 3s", "Exit code
  2"), never raw `key=value` text. An edit shows its diff with line numbers, a
  command or check shows the last lines it printed, an MCP call shows its text
  content; each is folded to a few rows and `ctrl+o` expands every tool line
  (and adds a dim row with the call id and the raw summary for cross-checking
  the session log). A click expands one line. A running tool has a dim bullet;
  one cut short by a stop says "Interrupted".
- While the agent works, KaroX's five pulsing dots sit right above the
  composer with what it is doing now in words ("Reading calc.py…", "Running
  tests …", "Thinking…", "Writing…"), the elapsed time, the tokens so far and
  how to interrupt. Under it: the plan step it is on ("Step 2 of 3: …"), a
  model-call retry when one happens, and tasks typed while it runs (Enter
  queues them; they start one after another when the run ends).
- Reasoning is never mixed into the answer: a dim `∴ Thought for 4s ·
  ctrl+r to show` line stands for it, and `ctrl+r` prints it.
- A finished run adds one compact result line: outcome, steps, files, checks,
  tokens, cost (when the model is priced), elapsed time; the reason, unmet
  requirements, the failure map and the changed files follow on their own
  lines only when there are any. Harness warnings (a repair prompt, the final
  checks, a restored regression, a refused command) become one readable
  notice each.
- The footer has a hint on the left (`? for shortcuts`, or the mode when it
  is not `build`) and the model, the tokens and — only near the window limit
  — the context share on the right. There is no idle/running word: the dots
  are the running state.

## Keys

| Key | Action |
| --- | --- |
| Enter | send the task (queue it while the agent runs); in a list, run or choose the highlighted row |
| `/` | open the command list (typing filters it) |
| `?` | in an empty composer, show or hide the shortcut list |
| Tab | in the command list, complete the command and keep typing its argument |
| ↑ / ↓ | move in a list; otherwise recall submitted lines (at the document edges) |
| Esc | close the list, the shortcuts or the key prompt; otherwise interrupt the agent (queued tasks return to the composer) |
| Shift+Tab | cycle the mode: build → plan → ideate |
| Ctrl+J | insert a newline |
| PageUp / PageDown | scroll the transcript (move by a page in a list); scrolled up, the view stays put until you scroll back to the end |
| Ctrl+O | expand or fold every tool line (verbose) |
| Ctrl+N | new session |
| Ctrl+R | print the last answer's reasoning |
| Ctrl+L | clear the screen |
| Ctrl+C | close what is open, else stop the agent, else clear the draft, else exit |
| Ctrl+Q | exit |

The keyboard never leaves the composer: while a list is open, the composer
forwards ↑/↓, PageUp/PageDown, Tab, Enter and Esc to it and what you type
filters it. Enter in a picker never sends its filter text as a task (a
pasted `/command` still runs). A recalled `/command` does not pop the list
open, so ↑ keeps walking the history. The mouse scrolls the transcript,
clicks list rows and expands tool lines.

The transcript follows its end — new lines and lines that grow while shown
(the streaming answer, a tool line gaining its preview) — as long as you are
at the end. Scrolling up with the wheel or PageUp stops that; scrolling back
to the end resumes it. A short transcript starts at the top.

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
/verify <command>      approve a verification command for this window
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
2. The runtime starts an agent only with a verification command the user
   approved; a command found in the repository (`package.json` scripts,
   pytest, `cargo test`, …) is a suggestion, never an approval. So when no
   check has been approved in this window yet, a submitted task waits and a
   picker asks "How should the changes be checked?": the detected commands
   by name, plus "Another command…" (which leaves `/verify ` in the
   composer). Picking one approves it — the same as typing
   `/verify <command>` — and the task starts right away; Esc gives the task
   back to the composer. A `/command` pasted while any picker is open runs
   as a command.
3. On submit it starts:

   ```text
   python -m karox.cli agent run --repository R --session-id S --task T \
       --stream-json [--continue-task] [--effort-level L] [--mode M] \
       [--verification-command JSON]... [--mcp-server ID=read|all]...
   ```

4. It reads stdout line by line. Each line is one JSON document:
   `{"type": "event", ...}` during the run and
   `{"type": "result", "report": {...}}` at the end. A document may carry
   `"v": <int>`; the client speaks version 1, decodes newer versions
   best-effort and ignores unknown fields and kinds, so a kernel that extends
   the stream never breaks an older screen.
5. Text deltas stream into the open answer stretch behind a caret; tool starts
   add one tool line that is updated when the matching `tool_finished`
   arrives; `test_started`/`test_finished` drive the live check tally;
   `step_finished` usage drives the token counter and the context note;
   `phase_changed` and `provider_retry` drive the working line; warnings
   about a tool call land on its tool line, other warnings, errors and
   compaction add a notice. `tool_started` carries the call's `detail` (the
   path, pattern, command or MCP arguments, bounded and redacted) and
   `tool_finished` carries its `summary` plus a short preview (diff hunks,
   output tail or MCP text) in `detail`.
6. After the run the client re-reads `session show --json` in the background:
   the durable record is the checkpoint state, and the status line shows when
   it was saved.
7. Esc or Ctrl+C stops the child. The session is durable, so the next task
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

`/mcp` lists configured servers with transport, whether the server is in this
session (and how many tools are allowed), liveness and a missing secret.
Choosing a server offers:

- **Probe the connection** — `mcp server inspect`: reachable or the server's
  own error, and the tool count.
- **Use in this session: all tools** — every tool the server lists becomes
  `allow`.
- **Use in this session: read-only tools** — tools marked read-only become
  `allow`, the rest `deny`.
- **Remove from this session** / **Remove the server** (asks in the same
  list).

The TUI never writes `ask`: nobody would be there to answer it, so a
mutating tool is either allowed or denied up front, and exact per-tool
decisions stay in `karox mcp session select`. Before the first task of a
window there is no session yet; the choice is remembered ("joins with the
first task") and sent with that task as `--mcp-server ID=all|read`, which
selects the server before the run starts. The last row — or `/mcp add` —
opens the form for a stdio command or a Streamable HTTP URL with
environment, headers, credential reference, OAuth, read-only tools and
timeout.

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
