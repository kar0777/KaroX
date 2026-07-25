# Agent tooling notes

This note records the contract for the hosted tools an external coding agent
needs in order to verify its own work, the reproducible launch command for the
bridge that exposes them, and the operational limits an agent has to plan
around.

## Interpreter and package layout

- Tests run under the same interpreter that imports the editable package:
  `C:\Users\ekono\AppData\Local\Programs\Python\Python313\python.exe`
  (CPython 3.13.11).
- `karox` is installed editable (`pip install -e .`) so subprocess invocations
  of `python -m karox.cli ...` resolve the package without a `PYTHONPATH`
  shim. Subprocess tests assert this contract; running the suite from a clean
  checkout requires the editable install.
- `requirements.txt` declares `mcp`, `PyYAML`, `keyring`, `httpx`, `httpx-sse`,
  `fastapi`, `uvicorn`, `pydantic`, `textual`, `rich` and `Pygments`. The last
  two are declared explicitly because `markdown_render` imports `rich.syntax`
  directly, which requires Pygments. Relying on them arriving transitively
  through `textual` is what previously produced a working install with a broken
  import.

## Bridge launch command (reproducible)

The bridge is started once and held. Restarting changes the credential the
agent stores, so it must be followed by a Notion reconnect.

Endpoint: `https://monsterpc.taila81286.ts.net/mcp` (Tailscale Funnel to
`127.0.0.1:8765`). Authorization uses a bearer credential held in the OS
keyring under `bridge/bridge-1784992825-f532f3`; the value is pasted only into
Notion's protected credential field and never into chat, logs, or this file.

Profile `notion`, protocol `mcp`, session `notion-elevated` (access profile
`elevated`, which grants the commit capability in addition to the
workspace-write ones; `workspace_write` does not allow `git.commit`).

```powershell
python -m karox.cli bridge serve `
  --profile notion --protocol mcp `
  --repository D:/проекты/KaroX-v5 `
  --session-id notion-elevated `
  --credential bridge-1784992825-f532f3 `
  --port 8765 --host 127.0.0.1 `
  --deadline-seconds 900 `
  --tool karox.repo.read_file `
  --tool karox.repo.write_file `
  --tool karox.repo.list_files `
  --tool karox.repo.search `
  --tool karox.checks.run `
  --tool karox.git.status `
  --tool karox.git.diff `
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
  --verification-command '["python", "-m", "karox.cli", "--help"]' `
  --verification-command '["python", "-m", "karox.cli", "paths", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "doctor", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "provider", "list", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "model", "list", "--json"]' `
  --verification-command '["python", "-m", "karox.cli", "bridge", "list", "--json"]'
```

### Why these eight tools

All eight Core tools are exposed so the external agent can read, search,
write, run approved checks, inspect git state, and commit (never publish to a
remote):

```
karox.repo.read_file    karox.repo.write_file   karox.repo.list_files
karox.repo.search       karox.checks.run        karox.git.status
karox.git.diff          karox.git.commit
```

`karox.git.commit` requires the `elevated` profile; under `workspace_write`
the bridge refuses to expose it, which is the intended guard.

### Deadline

`--deadline-seconds 900` raises the per-call ceiling from the 30 s default to
15 minutes. The Core command bound is `min(timeout_seconds, deadline_seconds)`,
capped at 3600 s, so a long check run no longer surfaces as `timed_out`. The
value flows from the CLI flag through `build_proxy_asgi_app` into each
`execute(...)` call, with no hardcoded override.

### Approved verification set

`karox.checks.run` only runs a command whose full argv exactly matches one of
the vectors supplied at launch (`core.py` membership test on a frozenset of
tuples, with no prefix or regular-expression matching). The vectors above
cover:

- 2 baseline vectors: `compileall` over `src tests`, and full discovery.
- 2 environment-recovery vectors: `pip install -r requirements.txt` and
  `pip install -e .`, so the agent can repair the interpreter the bridge
  depends on.
- One `unittest discover -p <test_*.py>` vector per test module, generated
  programmatically from `tests/test_*.py` so nothing is missed and the set
  stays closed.
- 6 diagnostic vectors that read CLI output for interface work. Note that
  `karox status` does not exist as a subcommand; `karox paths --json` is the
  closest available introspection command and is used instead.

The set is intentionally closed: adding a new approved command requires
restarting the bridge with an extended `--verification-command` list.

## Operational limits an agent must plan around

These were measured against the running bridge, not assumed.

- **A remote MCP client can time out long before Core does.** The Notion client
  abandons a tool call after roughly a minute, so the full-suite vector is not
  callable from it even with a 900 s deadline. Per-module vectors are the
  practical unit of work, and that is why every module has its own vector.
- **An abandoned call leaves the mutation lease held.** `checks.run` mutates, so
  it takes a lease with a time to live of the requested timeout plus ten
  seconds. If the client gives up, the session stays locked for the remainder of
  that window and every later mutating call fails with a session-locked error.
  Requesting a timeout close to what the work actually needs keeps that window
  short; requesting 900 s locks the session for about fifteen minutes.
- **Read-only tools are unaffected by the lease.** `repo.read_file`,
  `repo.list_files`, `repo.search`, `git.status` and `git.diff` keep working
  while a lease is held, so investigation can continue during a lockout.
- **Search results include ignored directories.** `repo.list_files` and
  `repo.search` walk the working tree without consulting ignore rules, so a
  virtual environment or build output can dominate the result budget. Narrow
  the glob (for example `src/karox/*.py`) until that is fixed.

## Hosted idempotency

The wire server used to demand a client supplied idempotency identifier in the
MCP metadata field. That field sits beside the argument object in the protocol
envelope, and many clients cannot set it. Strict Core argument validation also
rejects it when it is smuggled inside the arguments.

The practical effect was that every mutating tool was unreachable from such a
client. The server now generates a unique identifier when the client omits one,
so an accepted call still executes exactly once. A client that supplies a stable
identifier keeps full cross request replay protection, and an explicitly
supplied value still wins over the generated one.

An identifier derived from the argument content was deliberately rejected.
Rerunning the same test command after a code change would otherwise return the
previous cached outcome instead of a real run.

## `checks.run`

Already implemented in Core before this work; only the session allowlist and an
approved command set were missing. The bridge refuses to expose the tool unless
the caller passes an explicit set of approved command vectors, and any command
outside that set is rejected with an explicit error rather than being run. The
tool mutates, so it needs a mutation lease, which the bridge already handles.

## `git.commit`

Implemented as a Core tool and exposed through the bridge.

- Reuses the existing commit capability. No policy change, so commit stays
  available under the elevated profile only.
- Mutating, therefore a lease and an idempotency identifier are mandatory.
- Takes a message and an explicit non empty list of repository relative paths.
  Committing everything implicitly is not offered.
- Every path goes through the existing safe path resolver, so traversal,
  absolute paths, links, reparse points, and the Git and runtime metadata
  directories are rejected before Git runs.
- The message is length bounded, rejects control characters, and passes through
  the existing credential scanner.
- Two bounded Git invocations: stage exactly the approved paths, then commit
  restricted to those same paths.
- Repository supplied hooks are not executed, because running them implicitly
  would side step the process capability.
- A failed commit stays a failure. The command belongs to the set whose non zero
  exit or timeout makes the result not ok, so a model cannot report a commit
  that did not happen.
- Publishing to a remote remains impossible and is untouched by this tool.

## `repo.search`

Implemented as a Core tool and exposed through the bridge.

- Read only, reusing the repository read capability.
- Pure Python, so it needs no process capability and no external binary, and it
  behaves identically on all three operating systems.
- Takes a query, an optional repository relative glob to narrow candidates, an
  optional regular expression flag, an optional case sensitivity flag, and an
  optional result ceiling.
- Candidates are collected with the same confinement rules as the file listing
  tool, so metadata directories are never searched and links are skipped.
- Bounded by a candidate file ceiling, the existing per file byte ceiling, a
  result ceiling, and a per line length cap. Binary and non UTF-8 files are
  skipped instead of raising.
- Matched lines pass through the existing redaction helper before leaving the
  process.
- The result reports whether it was truncated, so a caller can tell a complete
  answer from a partial one.
- Known gap: ignored directories are still walked. See the operational limits
  section above.
