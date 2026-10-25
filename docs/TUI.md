# Terminal client

`karox` or `karox tui` opens the compact client. It is a Textual application
made of a small widget set and one stylesheet:

| File | Role |
| --- | --- |
| `tui/app.py` | application, layout, commands, run state, worker that runs one task |
| `tui/widgets.py` | header, chat log, user/assistant messages, tool card, notice, composer, footer |
| `tui/modals.py` | model picker, session picker, session panel, provider setup, MCP panel, skills, help, confirmation |
| `tui/backend.py` | subprocess bridge and the `--json` CLI calls |
| `tui/protocol.py` | versioned event/report decoding, tool categories, session-state parsing |
| `tui/i18n.py` | English/Russian message catalogue |
| `tui/prefs.py` | the `vnext/ui.json` slice the client owns |
| `tui/theme.tcss` | palette and layout |

## Layout

```text
KaroX · provider/model · 45.2k tok · ctx 82% · plan · effort high · proj · main · ● live
──────────────────────────────────────────────────────────────
  ▌ the task
    ✳ reasoning · 10.7k chars
    answer, streamed behind a caret, then rendered as markdown
    ○ repo.read_file · ok · 0.12s · read 12 lines
    ✎ repo.edit_file · ok · 0.42s · path=app.py
    ⬢ mcp.github.list_issues · running
  ────────────────────────────────────────────────────────────
  done · verified · 6 steps · files: 1 · checks: 2/2 · 44.0k · 35.3s
──────────────────────────────────────────────────────────────
  ⠋ › step 3 · checks.run · running
  Enter send · Ctrl+J newline · /help · Esc stop     working · step 3 · 12s · ✓1/✗0 · 45.2k tok
```

Nothing is boxed unless the user must act on it: a notice or a modal has a
border and a colour; ordinary prose does not. Tool cards carry a quiet left
border in the colour of their category (gold mutates, green proves, violet
leaves the process), and the header keeps the classic rule: the model is the
primary fact and is never dropped, non-defaults are stated and defaults are
not, the context note appears only near the window limit, and a field that no
longer fits disappears whole instead of being cut into a different-looking
value.

## Keys

| Key | Action |
| --- | --- |
| Enter | send the task |
| Up / Down | recall submitted tasks (at the document edges) |
| Ctrl+J | insert a newline |
| Esc | stop the running agent |
| F1 | help |
| F2 | model picker |
| F3 | session picker |
| F4 | MCP panel |
| F5 | session state panel |
| PageUp / PageDown | scroll the conversation |
| Ctrl+N | new session |
| Ctrl+R | show the last turn's reasoning |
| Ctrl+L | clear the conversation |
| Ctrl+C | stop a running agent, otherwise exit |
| Ctrl+Q | exit |

Mouse: the conversation scrolls under the wheel, a tool card opens its
unclipped detail on click, and every list, button and option in the modals is
clickable.

## Slash commands

```text
/provider        connect a provider and model
/model           choose a model
/sessions        list and continue sessions
/resume <id>     continue a specific session
/new             start a fresh session
/session         session state panel (also F5)
/mcp             MCP servers and tools
/skills          discovered repository skills and their detail
/verify <cmd>    add a verification command for this window
/unverify        clear the verification list
/effort <level>  auto | low | medium | high | extra-high | ultra
/mode <mode>     build | plan | ideate
/lang ru|en      interface language
/status          repository, model, session, effort, checks, run counters
/doctor          provider, credential, MCP and session health
/bridge          saved ChatGPT/Claude/HyperAgent bridges
/clear           clear the screen
/stop            stop the agent
/quit            exit
```

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
4. Text deltas stream into the answer card behind a caret; tool starts create
   one-line cards, categorised by canonical tool name (`read`, `edit`, `test`,
   `mcp`, `skill`, `plan`, `web`, `inspect`, `subagent`), that are updated when
   the matching `tool_finished` arrives; `test_started`/`test_finished` drive
   the live verification tally; `step_finished` usage drives the token counter
   and the context-occupancy note; `phase_changed` and `compacted` (a context
   checkpoint) surface on the activity line; warnings update the activity
   line; errors and compaction add a notice.
5. The result card is built from the report: status, steps, files, checks,
   tokens, elapsed time, and the reason when the run is not verified.
6. After the run the client re-reads `session show --json` once: the durable
   record is the checkpoint state, and the footer shows when it was saved.
7. The child is stopped with Ctrl+C or Esc. The session is durable, so the
   next task with `--continue-task` resumes it.

The client never parses human progress text, so changing a CLI message cannot
break a screen.

## Run and session state

`app.RunState` holds the live facts of the current or latest run — state word
(`idle | running | stopping | done | failed | stopped`), step, phase, elapsed,
tokens, context occupancy, and per-category tool counters (reads, edits,
checks passed/failed, MCP calls, skill loads, subagent calls, inspections,
edited paths). Every field comes from a typed stream event or the final
report; nothing is inferred from prose.

`protocol.SessionState` is the bounded, total parse of the durable session
record behind F5 `/session`: task and turn history, the model's plan
(todo/doing/done), rollback checkpoints, verification checks and the verified
snapshot step, provider usage and costs, MCP servers selected for the session,
loaded skills and subagent jobs. Sections whose record list is empty say
"none" instead of inventing content.

## Event fields

`tui/protocol.py` reads only fields the runtime already declares safe:

```text
kind, step, text_delta, reasoning_delta, reasoning_summary_delta,
tool, call_id, ok, summary, duration_seconds, reason, status, phase,
path, detail, usage
```

Private reasoning is counted, never shown as the answer. Ctrl+R opens it only
because the user explicitly asked.

## Modals

- **Session panel** (F5) projects the durable record described above. It is
  read-only and stays available during a live run.
- **Provider setup** selects a preset, fills the endpoint, provider id, model
  and key, then verifies live before saving. A loopback endpoint needs no key;
  a saved credential is reused when the CLI has one. A new key is sent to the
  CLI on stdin and stored by the OS keyring.
- **Model picker** lists registered models with the selected one marked. Model
  catalogs are refreshed from the CLI with `karox model discover --save`.
- **Session picker** lists only sessions bound to this repository; selecting
  one clears the screen, loads its checkpoint state and shows its task.
- **Skills** lists `karox skill list --json` discoveries; Enter opens the
  skill's description, when-to-use, permissions and files from `skill show`.
- **MCP panel** lists configured servers with transport, selection and liveness.
  `a` adds a stdio or Streamable HTTP server (environment, headers, credential
  reference, OAuth, read-only tools, timeout), `d` removes, `p` probes, and `s`
  selects the server: read-only tools become `allow`, mutating tools become
  `ask`. Exact per-tool decisions stay in `karox mcp session select`.
- `/bridge` lists saved inbound bridge profiles. Starting a bridge is a
  foreground process that owns a tunnel, so the modal points to the exact
  `karox bridge connect --saved NAME` command instead of hiding it in a worker.

## Testing

The client is tested through Textual's pilot with a recorded backend:
`tests/test_tui.py` feeds `Event` and `Outcome` objects and asserts on the
widgets, the run-state counters, the checkpoint refresh, the session panel,
responsive header fitting, narrow terminals, error and empty states, and the
emitter→decoder round trip against `cli._stream_json`. No model, public
network or real keyring write is involved. The live check is a separate smoke
run documented in [DEVELOPMENT.md](DEVELOPMENT.md).

## Language

The catalog is `tui/i18n.py`; keys fall back to English and then to the key
itself, so a missing translation is visible instead of crashing a screen. A
test pins the two catalogues to the same key set. The choice is stored in
`vnext/ui.json` under `language` and is shared with the CLI and the next
start.
