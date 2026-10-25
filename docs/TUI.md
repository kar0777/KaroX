# Terminal client

`karox` or `karox tui` opens the compact client. It is a Textual application
made of a small widget set and one stylesheet:

| File | Role |
| --- | --- |
| `tui/app.py` | application, layout, commands, worker that runs one task |
| `tui/widgets.py` | header, chat log, user/assistant messages, tool card, notice, composer, footer |
| `tui/modals.py` | model picker, session picker, provider setup, MCP panel, help, confirmation |
| `tui/backend.py` | subprocess bridge and the `--json` CLI calls |
| `tui/protocol.py` | event/report decoding |
| `tui/i18n.py` | English/Russian message catalogue |
| `tui/prefs.py` | the `vnext/ui.json` slice the client owns |
| `tui/theme.tcss` | palette and layout |

## Layout

```text
KaroX · repository · branch · provider/model · session · tokens
──────────────────────────────────────────────────────────────
  ▌ the task
    ✳ reasoning · 10.7k chars
    answer, streamed as plain text, then rendered as markdown
    → repo.read_file · running
    ✓ repo.edit_file · ok · 0.42s · path=app.py
  ────────────────────────────────────────────────────────────
  done · verified · 6 steps · files: 1 · checks: 2/2 · 44.0k · 35.3s
──────────────────────────────────────────────────────────────
  Enter send · Ctrl+J newline · /help · Esc stop          idle
```

Nothing is boxed unless the user must act on it: a notice or a modal has a
border and a colour; ordinary prose does not.

## Keys

| Key | Action |
| --- | --- |
| Enter | send the task |
| Ctrl+J | insert a newline |
| Esc | stop the running agent |
| F1 | help |
| F2 | model picker |
| F3 | session picker |
| F4 | MCP panel |
| Ctrl+N | new session |
| Ctrl+R | show the last turn's reasoning |
| Ctrl+L | clear the conversation |
| Ctrl+C | stop a running agent, otherwise exit |
| Ctrl+Q | exit |

## Slash commands

```text
/provider        connect a provider and model
/model           choose a model
/sessions        list and continue sessions
/resume <id>     continue a specific session
/new             start a fresh session
/mcp             MCP servers and tools
/verify <cmd>    add a verification command for this window
/unverify        clear the verification list
/effort <level>  auto | low | medium | high | extra-high | ultra
/mode <mode>     build | plan | ideate
/lang ru|en      interface language
/status          repository, model, session, effort, checks
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
   `{"type": "result", "report": {...}}` at the end.
4. Text deltas stream into the answer card; tool starts create one-line cards
   that are updated when the matching `tool_finished` arrives; warnings update
   the activity line; errors and compaction add a notice.
5. The result card is built from the report: status, steps, files, checks,
   tokens, elapsed time, and the reason when the run is not verified.
6. The child is stopped with Ctrl+C or Esc. The session is durable, so the next
   task with `--continue-task` resumes it.

The client never parses human progress text, so changing a CLI message cannot
break a screen.

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

- **Provider setup** selects a preset, fills the endpoint, provider id, model
  and key, then verifies live before saving. A loopback endpoint needs no key;
  a saved credential is reused when the CLI has one. A new key is sent to the
  CLI on stdin and stored by the OS keyring.
- **Model picker** lists registered models with the selected one marked. Model
  catalogs are refreshed from the CLI with `karox model discover --save`.
- **Session picker** lists only sessions bound to this repository; selecting
  one clears the screen and shows its task.
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
widgets. No model, public network or real keyring write is involved. The live
check is a separate smoke run documented in [DEVELOPMENT.md](DEVELOPMENT.md).

## Language

The catalog is `tui/i18n.py`; keys fall back to English and then to the key
itself, so a missing translation is visible instead of crashing a screen. The
choice is stored in `vnext/ui.json` under `language` and is shared with the CLI
and the next start.