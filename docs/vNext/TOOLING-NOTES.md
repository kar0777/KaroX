# Agent tooling notes

This note records the tools an external coding agent needs to do real work and
verify it, the reproducible launch command for the bridge that exposes them, and
the operational limits the agent has to plan around.

## Interpreter and package layout

- Tests run under the same interpreter that imports the editable package:
  `C:\Users\ekono\AppData\Local\Programs\Python\Python313\python.exe`
  (CPython 3.13.11).
- `karox` is installed editable (`pip install -e .`) so subprocess invocations
  of `python -m karox.cli ...` resolve the package without a `PYTHONPATH`
  shim. Subprocess tests assert this contract; running the suite from a clean
  checkout requires the editable install.
- `requirements.txt` and the `dependencies` list in `pyproject.toml` are kept in
  step. Both declare `rich` and `Pygments` explicitly because
  `markdown_render` imports `rich.syntax` directly. Relying on them arriving
  transitively through `textual` is what previously produced a working install
  with a broken import, and the wheel built in CI would have reproduced it.

## The eleven exposed tools

```
karox.repo.read_file    karox.repo.read_lines   karox.repo.list_files
karox.repo.search       karox.repo.write_file   karox.repo.edit_file
karox.checks.run        karox.git.status        karox.git.diff
karox.git.log           karox.git.commit
```

The first three read, the next one searches, the next two write, then checks run,
Git state and history are readable, and a commit can be recorded. Publishing to a
remote is absent by design and stays impossible.

`karox.git.commit` requires the `elevated` profile; under `workspace_write` the
bridge refuses to expose it, which is the intended guard.

### Why editing and windowed reads matter

`repo.write_file` replaces a whole file. For a module of several thousand lines
that is not a usable edit primitive: the caller must reproduce the entire file to
change three lines, and one transcription slip silently corrupts working code.
The interface modules are exactly that large, so without `repo.edit_file` they
could not be touched safely at all.

`repo.edit_file` replaces an exact string and requires the caller to declare how
many occurrences it expects. An ambiguous or absent match is refused before
anything is written. It reuses the audited atomic writer, so permissions, fsync,
temporary file cleanup, the size ceiling and the credential scanner behave
exactly as for `repo.write_file`.

`repo.read_lines` returns a bounded window, so a large module can be inspected
region by region instead of in full.

Both live in `ExtendedCoreRuntime` in `core_tools.py` rather than in `core.py`,
so the audited boundary file is untouched. Every call still goes through
`CoreRuntime.execute`, which keeps policy, repository confinement, mutation
leases, idempotency and evidence on the path.

### Ignored directories

`repo.list_files`, and therefore `repo.search`, skip dependency and build
directories: virtual environments, caches, `node_modules`, `build`, `dist` and
egg-info. The earlier walk included everything, so a single search in a real
checkout scanned over a thousand vendored files and truncated the actual source
out of the answer. Filtering runs before the result ceiling is applied, so
dependencies cannot crowd out real matches.

### Screenshots of the interface

`scripts/tui_screenshot.py` drives the interface through Textual's own test pilot
and writes SVG. No terminal, display or input device is involved, so it is
reproducible in CI.

SVG was chosen deliberately: it is UTF-8 text, so a captured frame can be read
back with `repo.read_file` and the layout reviewed without a viewer. Every glyph,
colour and cell position is present in the markup, which is what terminal layout
review actually needs.

```
python scripts/tui_screenshot.py --out docs/tools/screenshots/start-ru.svg
python scripts/tui_screenshot.py --language en --out docs/tools/screenshots/start-en.svg
python scripts/tui_screenshot.py --keys ctrl+b --out docs/tools/screenshots/bridge.svg
```

Keys are sent in order before the capture, using Textual's key names, which is
how a specific screen is reached.

## Bridge launch command (reproducible)

The bridge is started once and held. A restart is required for any change under
`src/karox/` to take effect, because the running process has already imported the
modules; editing files on disk does not reload them.

Endpoint: `https://monsterpc.taila81286.ts.net/mcp` (Tailscale Funnel to
`127.0.0.1:8765`). Authorization uses a bearer credential held in the OS keyring
under `bridge/bridge-1784992825-f532f3`; the value is pasted only into the
client's protected credential field and never into chat, logs, or this file.

Profile `notion`, protocol `mcp`, session `notion-elevated`, access profile
`elevated`.

```powershell
python -m karox.cli bridge serve `
  --profile notion --protocol mcp `
  --repository D:/проекты/KaroX-v5 `
  --session-id notion-elevated `
  --credential bridge-1784992825-f532f3 `
  --port 8765 --host 127.0.0.1 `
  --deadline-seconds 900 `
  --tool karox.repo.read_file `
  --tool karox.repo.read_lines `
  --tool karox.repo.write_file `
  --tool karox.repo.edit_file `
  --tool karox.repo.list_files `
  --tool karox.repo.search `
  --tool karox.checks.run `
  --tool karox.git.status `
  --tool karox.git.diff `
  --tool karox.git.log `
  --tool karox.git.commit `
  --verification-command '["python", "-m", "compileall", "-q", "src", "tests"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]' `
  --verification-command '["python", "-m", "pip", "install", "-r", "requirements.txt"]' `
  --verification-command '["python", "-m", "pip", "install", "-e", "."]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_agent.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_benchmark.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_bridge.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_core.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_credentials.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_ecosystem.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_extended_core_tools.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_handoff.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_hosted_bridge.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_markdown_render.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_mcp.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_migration_cli.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_packs.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_policy.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_promptql_outbound.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_provider_adapters.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_provider_cli.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_providers.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_registry.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_routing.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_sessions.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_skill_cli.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_skills.py"]' `
  --verification-command '["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_tui.py"]' `
  --verification-command '["python", "scripts/tui_screenshot.py", "--out", "docs/tools/screenshots/start-ru.svg"]' `
  --verification-command '["python", "scripts/tui_screenshot.py", "--language", "en", "--out", "docs/tools/screenshots/start-en.svg"]' `
  --verification-command '["python", "scripts/tui_screenshot.py", "--keys", "ctrl+b", "--out", "docs/tools/screenshots/bridge.svg"]' `
  --verification-command '["python", "-m", "karox.cli", "--help"]' `
  --verification-command '["python", "-m", "karox.cli", "paths", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "doctor", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "provider", "list", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "model", "list", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "bridge", "list", "--json"]'
```

### Approved verification set

`karox.checks.run` only runs a command whose full argv exactly matches one of the
vectors supplied at launch: a membership test on a frozenset of tuples, with no
prefix or regular-expression matching. The vectors above cover the two baseline
checks, two environment-recovery installs, one discovery vector per test module,
three screenshot captures, and six read-only CLI diagnostics.

The set is intentionally closed. Adding a command means restarting the bridge
with an extended list, which is also why a new test module cannot be run until
the next restart.

Starting the bridge is itself outside the approved set on purpose. A long-lived
server is not a bounded check, and an agent should not be able to spawn one.

## Operational limits an agent must plan around

These were measured against the running bridge, not assumed.

- **A remote client can time out long before Core does.** The client abandons a
  tool call after roughly a minute, so the full-suite vector is not callable from
  it even with a 900 s deadline. Per-module vectors are the practical unit of
  work, which is why every module has its own vector.
- **An abandoned call leaves the mutation lease held.** `checks.run` mutates, so
  it takes a lease with a time to live of the requested timeout plus ten seconds.
  If the client gives up, the session stays locked for the remainder of that
  window and every later mutating call fails with a session-locked error. Asking
  for a timeout close to what the work actually needs keeps that window short;
  asking for 900 s locks the session for about fifteen minutes.
- **Read-only tools are unaffected by the lease**, so investigation can continue
  during a lockout.
- **Source changes need a restart.** The bridge process has already imported
  `src/karox/`, so editing a module on disk changes nothing for the live tools.
  Tests run in fresh subprocesses and do pick up changes, which makes the test
  suite the working verification channel between restarts.

## Hosted idempotency

The wire server used to demand a client supplied identifier in the MCP metadata
field. That field sits beside the argument object in the protocol envelope, and
many clients cannot set it, while strict Core argument validation rejects it when
it is smuggled inside the arguments. Every mutating tool was therefore
unreachable from such a client.

The server now generates a unique identifier when the client omits one, so an
accepted call still executes exactly once. A client that supplies a stable
identifier keeps full cross request replay protection, and an explicitly supplied
value wins over the generated one.

An identifier derived from the argument content was deliberately rejected.
Rerunning the same test command after a code change would otherwise return the
previous cached outcome instead of a real run.

## Tool notes

### `checks.run`

Already implemented in Core before this work; only the session allowlist and an
approved command set were missing. Any command outside the approved set is
rejected with an explicit error rather than being run. The tool mutates, so it
needs a mutation lease, which the bridge handles.

### `git.commit`

- Reuses the existing commit capability, so no policy change was needed and
  commit stays available under the elevated profile only.
- Takes a message and an explicit non empty list of repository relative paths.
  Committing everything implicitly is not offered.
- Every path goes through the safe path resolver, so traversal, absolute paths,
  links, reparse points, and the Git and runtime metadata directories are
  rejected before Git runs.
- The message is length bounded, rejects control characters, and passes through
  the credential scanner.
- Two bounded Git invocations: stage exactly the approved paths, then commit
  restricted to those same paths.
- Repository supplied hooks are not executed, because running them implicitly
  would side step the process capability.
- A failed commit stays a failure, so a model cannot report a commit that did not
  happen.

### `repo.search`

- Read only, pure Python, so it needs no process capability and no external
  binary and behaves identically on all three operating systems.
- Takes a query, an optional repository relative glob, an optional regular
  expression flag, an optional case sensitivity flag and a result ceiling.
- Bounded by a candidate file ceiling, the per file byte ceiling, a result
  ceiling and a per line length cap. Binary and non UTF-8 files are skipped
  instead of raising.
- Matched lines pass through the redaction helper before leaving the process.
- Reports whether the answer was truncated, so a caller can tell a complete
  answer from a partial one.

### `git.log`

Reports recent history, which neither `git.status` nor `git.diff` could. A
repository with no commits makes Git exit non-zero, and the result is reported as
not ok rather than as empty history.
