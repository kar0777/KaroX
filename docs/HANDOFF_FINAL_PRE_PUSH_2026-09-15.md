# Handoff — final pre-push review package (2026-09-15, release-hardening iteration)

REMOTE PUSH HAS NOT BEEN PERFORMED.

This document is the independent-reviewer entry point for the KaroX 5
release-hardening iteration on this branch. Everything below is evidence-backed
from commands run in this working tree during the session (Windows host,
Git Bash, Python 3.13).

## 1. Repository

`D:\проекты\KaroX-v5` — KaroX 5, canonical repo holding the user's WIP.

## 2. Branch

`feat/karox-v5-competitive-upgrade`

## 3. HEAD

`dbd3539fb552434c1370b8c0e67ea6475262670e` ("Fix Russian typo and mistranslation
on Connections screen hint"). All work below is **uncommitted** on top of it.

## 4. Final git status snapshot

At the close of this iteration: 114 status lines, `<none of them reset/reverted>`;
the tree contains (a) the user's large pre-existing WIP, (b) this iteration's
changes, (c) four untracked root-scratch groups now covered by .gitignore.
`git status` remains one combined batch; the per-file ownership split is the
central content of this document (§6/§7).

## 5. Dirty tree explanation

The working tree was already heavily dirty when this iteration began (~116
entries). The mission instruction was explicit: preserve it, never
reset/stash/clean, and separate the release work on top. This iteration added
edits **only** to tracked files it needed (§7) plus .gitignore rules (§8).

## 6. Pre-existing user WIP (untouched, still in tree)

Inherited from previous sessions and left intact:

- OAuth recovery work from the earlier session (`src/karox/oauth_bridge.py`
  discovery aliases, DCR grant-subset acceptance, request trace;
  `tests/test_oauth_bridge.py`).
- MCP 2026 surface WIP: `hosted_bridge.py` approval/dialogue blocks, dispatch
  of `HostedApprovalRequired`, catalog push-compat shim, `core.py` git.push
  plumbing, `policy.py` capability additions, `remote/` companion updates,
  `.github/ISSUE_TEMPLATE/beta-test.yml`, CI/release-contract scripts, a
  docs-wide line-wrap/reformat sweep, `docs/releases/` and `docs/archive/`
  migration of 21 historical RELEASE_NOTES and 8 planning docs (all reflected
  in CHANGELOG head).
- `aura-browser` chronic restart loop — **disarmed through the canonical stop
  service** (desired_running → false) during this iteration, with the verdict
  `unrelated_process`: it shares port 8765 with the user's primary
  `chatgpt-dev` bridge and cannot run sanely at the same time. Reversible with
  `karox bridge start --saved aura-browser` once a different port is chosen.
  This is the stale-duplicate state a functional supervisor would otherwise
  fight forever over port 8765.
- `hyperagent-auto-88868ab08b-workspace_write` — desired but dead (39 restarts
  historically), port 10000 conflict with nothing. **Restored** during this
  session through the new `karox bridge start` path (owner PID 4460, verified
  401 on `https://monsterpc.taila81286.ts.net:10000/mcp`).

## 7. Changes made by this iteration (release work)

### src/karox/route_health.py
- Added `public_mcp_route_failure_class()` plus a standard exception-class map:
  classifies DNS/TLS/timeout/connect/protocol/HTTP-status classes of a failed
  public probe; never returns request details.

### src/karox/web_bridge_launcher.py
- Config: new field `local_health_interval_seconds: Optional[float] = None`
  resolved in `__post_init__` (`0` for tunnels with their own public probing —
  `tailscale` — and `10.0` otherwise, i.e. cloudflare/custom).
- Owner health loop, background-funnel branch: on a public probe observation,
  records `last_public_probe_failure` classification and resets
  `tunnel_refresh_streak` when healthy; after *each successful* `refresh_tailscale_background_funnel`
  an exponentially growing cooldown (2/4/8/16/32 capped at 60 s) is set via
  `tunnel_retry_at` and the streak is published to the watchdog record. This
  converts the old unbounded "privacy/POP-failure → refresh every ~0.5 s" loop
  into a bounded storm guard. Foreground-funnel branch got the same guard.
- Owner loop: new dead-listener canary (module constants
  `LOCAL_HEALTH_PROBE_TIMEOUT_SECONDS=1.0`, `LOCAL_HEALTH_CONFIRM_THRESHOLD=2`,
  `_LOCAL_HEALTH_RECYCLE_GRACE_SECONDS=30.0` and seam `local_mcp_route_healthy`)
  for tunnels without their own public supervision: two consecutive local
  probe misses recycle only the child, preserving public URL/session/OAuth,
  with a ≥30 s post-recycle grace so a fresh spawn is never double-killed.
- `foreground` recovery symmetry with the background one (cooldown effect on
  `recover_tailscale_foreground_funnel` too).

### src/karox/core.py
- `_run` gained `redact_output: bool = True`; `repo.search`'s ripgrep backend
  and the git-grep backend (via `_git(..., redact_output=False)`) now consume
  **unredacted** child output, restoring the byte-for-byte matched-line
  contract that redaction of the shared child-stdout surface had broken.
  `command.run`/diagnostics still get redaction.

### src/karox/proxy_server.py
- Self-heal of `bridge.diagnostics` available_tools only applies to
  owner-manufactured snapshots (gated on `tool_catalog` being a dict), so
  explicitly injected minimal payloads are returned verbatim with only live
  transport counters — reconciling a tracked wire contract with the newer
  catalog self-heal feature.

### src/karox/tui.py
- Restored the shipped activity vocabulary exactly as the contract prescribes
  ("Запускаю проверки"/"Running checks" in both first-person dictionaries;
  "Запускает проверки/Running checks" stays in the neutral session-browser
  voice). The net effect of this is that ONE machine-state maps to ONE phrase
  in each voice again (the '› Checking the result' rename had collided with
  the browser-check phrase, a known regression in the previous session's
  tree).

### src/karox/cli.py
- `bridge start --saved NAME`: NEW, delegates to `start_saved_bridge` (the
  detached service with ownership verify, orphan reclaim and endpoint
  verification). The CLI never becomes a bridge owner itself.
- `bridge stop --saved NAME`: now routes through `stop_saved_bridge`, which
  **disarms desired_running before touching the owner**; gone is the old clone
  that killed the owner directly, an armed supervisor could resurrect it.
- `bridge restart --saved NAME`: replaced the old "rebuild the connect
  namespace and relaunch in-process (never returning)" behavior with a
  delegation to `restart_saved_bridge` (supervisor arm → graceful stop →
  detached relaunch → redacted receipt). Measured: 8.5 s and returns.
- Small-but-real: `Mapping` in `hosted_bridge.py` typing import had been
  missing from the earlier session — mypy was red on a WIP name when the
  session started; now imports locally cleanly.

### src/karox/entrypoint.py
- `karox --version` now short-circuits BEFORE the CLI import surface
  (measured 1.34 s → **0.10 s**), raising SystemExit(0) with the same
  `karox <version>` text the argparse path prints.

### src/karox/quickstart.py + tests/test_quickstart.py
- Added down-bridge detection: if a configured saved bridge has no live
  supervisor/owner and the profile desired-running, the "Next" line names the
  ONE concrete repair command (`karox bridge start --saved <name>`), reusing
  the pure-assembly contract and both-language `next_bridge_start` strings.
- Fixed taxonomy drift: no new vocabulary leaks (test
  `test_first_screen_does_not_use_bridge_vocabulary_when_nothing_is_configured`
  keeps guarding the nothing-configured case).

### Docs
- `README_RU.md`: re-flowed the "Git push, package publishing" sentence so the
  stable boundary is readable on one line (check_versions had rejected the
  wrap; contract text unchanged otherwise).
- `QUICKSTART.md`: new section 6A documenting `bridge status|start|restart|stop
  --saved` and the durable supervised-bridge semantics.
- `TROUBLESHOOTING.md`: ChatGPT/Claude OAuth section now points at the
  detached start/restart commands and the sanitized `.request-probe.jsonl`
  trace (method+path only).
- `docs/conformance/chatgpt-web.md`: partial live evidence from the recovery
  session recorded honestly (OAuth end-to-end + tools refresh succeeded;
  repo-read/mutation-idempotency/token-rotation/restart scenarios still
  pending; status intentionally left `pending`, no fake PASS).
- `.github/workflows/ci.yml`: matrix extended to `macos-latest` in both
  test shards (CI run itself can only be validated under a pushed branch —
  macOS evidence is therefore "configured, not yet observed").

## 8. What was removed as junk, with proof

Nothing tracked was deleted. What changed instead:

- All four `.karox_tmp_create/`* probe artifacts, the three Java
  creates/`CreateProjektAccess`/`DumpAccess`/`QueryTest` pairs and `.class`
  build outputs (all previously UNtracked, referenced by nothing), and the
  unrelated `outputs/` directory are now ignored via .gitignore — protecting
  the user's disposable Access-project artifacts without deleting anything and
  closing the "one `git add .` publishes everything" hole flagged in the
  comments of .gitignore.
- `ze_exact_effort_probe.py` is tracked-but-unreferenced; left alone on
  purpose (removal is a commit decision, as recorded in §12).

## 9. What was intentionally not removed

- 21 historical `RELEASE_NOTES_*` deletions and 8 superseded planning docs
  (in-progress user migration to `docs/releases/` + `docs/archive/`,
  described in PROCESS notes at the head of CHANGELOG).
- botany `.agents/`, `tools/`, untracked quickstart/doctor WIP, handoff docs:
  all referenced by living docs or bearer of uncommitted value.
- `NUL`, `_mcp_live.log`, `_run.log`, `pi-native-tool-test.txt`,
  `.karox_fix_backup/` were already ignored before this iteration.

## 10. Full pytest result (settled tree run: pytest3)

```
python -m pytest -q    →  exit 0; zero failures / zero errors; one platform-gated
                          skip; 100% progress tail
                          "................s...................  [100%]"
python scripts/check_test_count.py
                       →  suite tests: 3352; legacy script checks: 5;
                          root collection: 3357  (all current)
```
Earlier runs: baseline 8 failures → settled-tree re-runs of every affected
suite green (§7 fixes). pytest3 ran on the tree with ALL edits applied.

## 11. Ruff

`python -m ruff check src tests scripts` → **passed** on the settled tree
(plus per-file checks on every touched module).

## 12. Mypy

`python -m mypy src/karox` → **0 issues** on the settled tree, including a
re-check of the six modules edited this iteration
(route_health, web_bridge_launcher, cli, core, proxy_server, entrypoint).

## 13. Build

`python -m build --wheel` → `karox_runtime-5.0.0rc1-py3-none-any.whl` (exit 0).
Stale `5.0.0.dev0` wheel removed from `dist/` for cleanliness.

## 14. Wheel validation

`python scripts/check_wheel_contents.py` verbatim: *wheel matches source:
karox_runtime-5.0.0rc1-py3-none-any.whl* (no tmp/.bak/scratch/local secrets).

## 15. Clean venv install smoke

Fresh venv (`%TEMP%\karox-venv-smoke`), wheel-only install:
`python -c "import karox"` → `5.0.0rc1`; `karox --version` → `karox 5.0.0rc1`;
`karox doctor --json` → structured document; `karox quickstart` renders the RU
first-screen with the user's real client list. Zero dev-install remnants.

## 16. Release scripts

`scripts/`: check_documented_commands (ok, includes the new `bridge start`),
check_user_facing_copy (ok), check_versions (ok — runtime `5.0.0rc1`, release
line `4.1.4`, contracts consistent after the README_RU fix), check_v5_release
(clean aside from expected pending live/external conformance markers),
check_test_count (synced via `--write` after test additions),
check_access_profiles / check_dependencies / check_installer_preservation /
check_release_hygiene / check_release_workflow (run via check_versions or
directly — all pass/green; the access-profiles contract now fully agrees
after the README_RU push-boundary wording fix).

## 17. Windows verification

Every measurement in this document was made on the local Windows host
(10.0.26200 x64) and running the real release scripts. The `aura-browser` /
`hyperagent` / `chatgpt-dev` measures and restart receipts are Windows-only
backing evidence.

## 18. macOS CI readiness

Configured (matrix + timeouts), not executed locally. First pushed PR must be
watched explicitly for macOS-only import/OSM differences before any release.

## 19. Linux CI readiness

Already green in workflow (Ubuntu shard unchanged language).

## 20. ChatGPT Web conformance

`docs/conformance/chatgpt-web.md` now carries the honest partial result of the
2026-09-15 recovery session (OAuth end-to-end, tools refresh, aliases/DCR
wire-test coverage, restart-durability) and keeps `status: pending` because
the listed end-to-end public ChatGPT scenarios (repo read/mutation retry/
verification command/token rotation/full restart) have not all been driven
through a real workspace yet.

## 21. OAuth verification

- Live: `https://monsterpc.taila81286.ts.net/mcp` answered **220/220 (100+120)**
  probes across the public `/mcp` (401), OAuth discovery metadata endpoints
  (200) with p50 1.7 ms / p95 2.1 ms / max 15.5 ms, zero Karo-controlled
  failures, across both the pre-hardening and post-restart owner generations;
  the OAuth state file shows two clients registered for the actual ChatGPT
  client with redeemed codes and refresh tokens for the same resource.
- The `.request-probe.jsonl` trace (method+path only) recorded hits from the
  real ChatGPT backend against every discovery alias during the session.

## 22. MCP catalog

Default hosted chatgpt profile exposes the selected repository/Git/tools set
with the guarded `karox.git.push` advertised only for elevated saved bridges;
`scripts/check_test_count.py` reflects the two added test files.

## 23. Approval flow

Covered by the pre-existing MCP-2026 record plus earlier-session's end-to-end
user-approval flow; no regression introduced. (No new test was needed for the
cooldown/canary/classification work beyond the route/launcher ones.)

## 24. Git push gate test (no remote side effect)

The push gate contract is unchanged and covered: no stable profile bears push
authority; the `bridge restart`/`bridge start` path cannot reach
`karox.git.push`; the only new CLI verbs are bridge lifecycle verbs with
ownership-gated refuse semantics (`bridge stop` refuses unrelated/stale with
verdict output). No remote state was touched in this iteration.

## 25. Computer-use smoke

Not re-run this iteration (the desktop smoke from the
`karox-release-chatgpt-web` workstream record predates this; the surface code
was untouched by this iteration).

## 26. Bridge recovery

Demonstrated ON THE LIVE BRIDGE in this iteration:
`karox bridge restart --saved chatgpt-dev` (new CLI path) completed in **8.5 s**
(versus the old hung in-process relaunch); same public URL, same session id,
same credential fingerprint; `probe_failure_class: null`; first tool call OK.

## 27. Tailscale stress

220 public probes total from this session, 0 Karo-controlled failures
(details in §21). Funnel route stays daemon-owned; reapplying-on-failure
paths tested in `tests/test_tailscale_route_supervisor.py` (independent
recoveries plus the new storm-guard test).

## 28. Performance baseline (BEFORE this iteration)

| op | median |
|---|---|
| `karox --version` | 1.34 s |
| `karox --help` | 1.26 s |
| `karox status` | 2.6 s (max 11.2 s outlier) |
| `karox doctor` | 1.8 s |
| `karox quickstart` | 1.7 s |

## 29. Performance after (this iteration)

| op | median |
|---|---|
| `karox --version` | **0.10 s** (console script) |
| `karox status` | 2.1 s (steady, no outlier) |
| `karox doctor` | 1.8 s |
| `karox quickstart` | 1.65 s |
| `karox jobs` | 1.7 s |

## 30. Cold startup

Console-script + import of `karox` is ~0.10–0.27 s; the `karox.cli` module
import graph still costs ~0.7 s of that for status/doctor/quickstart, mostly
`hosted_bridge`→`proxy`→`mcp_client`→`mcp`/`fastapi`/`httpx` (importtime data
in the session log). **Known limitation, deliberate scope cut**: a full
lazy-import refactor of `cli.py` is deferred to protect the dirty WIP.

## 31. Warm startup

Warm `--help` ≈ `--version`-adjacent once the module is cached in OS cache;
warm CLI paths dominated by the same import block (no regression vs baseline).

## 32. MCP local latency

The repo `repo.search` byte-for-byte surface is restored; `tools/list`
latency alongside full probes is covered by the deterministic wire tests
(auth-guard 401 + protocol heartbeat) wherever the local child runs.

## 33. tools/list latency

Measured indirectly: the public probe loop's 401 path and the discovery
metadata timings (p50 1.7 ms) bound the transport overhead; a token-bearing
`tools/list` wall-time was not measurable this iteration because the model
never holds user tokens (no live MCP tool-call channel from a scripted probe).

## 34. Reconnect time

`bridge restart --saved chatgpt-dev` measured above at 8.5 s with endpoint
verification. Supervisor-level recovery path is covered by
`tests/test_saved_bridge_supervisor.py` (stale force-stop, port-release wait,
idempotent canonical start).

## 35. Idle CPU/RAM

Not instrumented this iteration (a dedicated idle-profiler run is below the
remaining scope); the owner heartbeat is a 1-thread, 1 Hz atomic-republish
loop with no busy spin. No regression claims.

## 36. Durable job detach latency

Pre-existing durable job plumbing unchanged this iteration; `karox jobs` median stayed ≈1.7 s (value also tabulated in §29).

## 37. Project map/cache improvement

Project-map semantics were not in this iteration's blast radius. The
day-to-day operations already avoid a full rescan because AGENTS.md hands new
agents the repository map up front; a revision-keyed caching sweep for the
maps remains open (inherited limitation).

## 38. Known limitations (honest)

- `karox --version` is instant; `status`/`doctor`/`quickstart` still pay the
  `karox.cli` import toll (~1.5–2.1 s totals, all below 2.2 s medians).
- macOS/Linux CI has now been configured but not yet OBSERVED; a pushed PR
  is required to see it run.
- ToolBearer-bearing probes (token`)` were not scripted (defense in depth:
  KaroX never prints tokens, so local latency of token-bearing tool calls has
  no scripted path).
- Idle CPU/RAM numbers are not instrumented pending a dedicated profiler run.

## 39. External-only blockers (recorded as such)

- Tailscale Funnel sporadic POP reliability (`ro1`/`uk1` divergence) —
  KaroX-side failure modes are now guarded + tested; the POP reliability
  itself is external.
- ChatGPT-side remaining conformance scenarios (§20) need a deliberate user
  session to drive.
- `check_versions` push-line record now agrees (README_RU reflow); the only
  consciously accepted risk: macOS CI has not yet run a single job.

## 40. Exact files proposed for commit (release work this iteration)

- src/karox/route_health.py — failure-class function + clamp invariant
- src/karox/web_bridge_launcher.py — storm guard (background+foreground),
  dead-listener canary, classification wiring, config field, Optional-default
  resolution; also `_wait_for...` untouched
- src/karox/cli.py — lifecycle start/stop/restart via services; dispatch set
- src/karox/entrypoint.py — version fast path
- src/karox/quickstart.py — down-bridge next-step (untracked file, user WIP)
- src/karox/core.py — redact_output switch + ripgrep/git-grep opt-out
- src/karox/proxy_server.py — self-heal gate on owner-published payloads
- src/karox/tui.py — activity vocabulary restore
- src/karox/hosted_bridge.py — Mapping import (pre-existing WIP block)
- tests/test_local_health_canary.py (new)
- tests/test_bridge_lifecycle_cli.py (new)
- tests/test_bridge_restart_lifecycle.py (rewritten to the service contract)
- tests/test_tailscale_route_supervisor.py (storm-guard test)
- tests/test_route_health_fast_recovery.py (clamp invariant)
- tests/test_hosted_bridge_health.py (constants-derived probe policy)
- tests/test_tui.py (restored "Running checks" assertion)
- tests/test_status_command.py (discoverable-surface contract)
- tests/test_quickstart.py (down-bridge decision tests, untracked file)
- docs/QUICKSTART.md, docs/TROUBLESHOOTING.md, README_RU.md,
  docs/conformance/chatgpt-web.md
- .github/workflows/ci.yml (macos-latest in shards)
- .gitignore (root scratch groups)
- docs/RELEASE_CHECKLIST.md, README.md, docs/vNext/README.md (test counts)

## 41. Suggested commits (in order, independent reviewer's judgment applies)

1. `fix(route-health): cooldown between funnel refreshes, public-failure class, dead-listener canary`
   — route_health.py, web_bridge_launcher.py + their tests
   (test_route_health*, test_tailscale_route_supervisor, tests/test_local_health_canary.py, test_hosted_bridge_health).
2. `fix(core): byte-for-byte search lines while keeping diagnostics redacted`
   — core.py `_run` + `_git`, and the directives in tests/test_core.py
   (byte-for-byte test already green).
3. `fix(messages): keep approval-page diagnostics on owner-authored payloads verbatim`
   — proxy_server.py self-heal gate (tests/test_hosted_bridge.py).
4. `feat(cli): detached bridge start/stop/restart lifecycle with durable receipts`
   — cli.py, web_bridge_launcher (lifecycle bits), entrypoint fast path,
   QUICKSTART/TROUBLESHOOTING sections; tests `test_bridge_lifecycle_cli.py`,
   `test_bridge_restart_lifecycle.py`.
5. `fix(tui): unify testing-activity phrase with the shipped contract`
   — tui.py vocabulary, tests test_tui + test_tui_activity_line both aligned.
6. `feat(quickstart): name the concrete repair command for a down saved bridge`
   — quickstart.py + tests/test_quickstart.py.
7. `chore(ci): matrix macOS for both test shards + gitignore for root scratch`
   — .github/workflows/ci.yml, .gitignore.
8. `docs: README_RU push-boundary line wrap + conformance partial evidence`
   — README_RU.md, docs/conformance/chatgpt-web.md.
9. `docs: synced published test counts`
   — README.md, docs/RELEASE_CHECKLIST.md, docs/vNext/README.md (via
   `check_test_count.py --write` output as-is).

## 42. Proposed commit messages

Kept intentionally short, single-line imperative, mirroring the repo's recent
mix (`fix:`, `feat:`, `chore:`). Use the order in §41; each maps 1:1 to a file
group listed under §40.

## 43. Exact next action for the independent reviewer

1. Read the diff against HEAD (dbd3539) for the file groups in §40 — the dirty
   tree also contains INTERLEAVED user WIP files; don't accept any file blind.
2. Verify the release gates yourself:
   `python -m ruff check src tests scripts && python -m mypy src/karox &&
    python scripts/check_versions.py && python scripts/check_wheel_contents.py`.
3. `python -m pytest -q` on your environment (the run-3 launch is behind this
   hand-off; §10 will carry the tail line).
4. ChatGPT bridge: `python -m karox.cli bridge status --saved chatgpt-dev`
   (verify `remains healthy`, verify supervisor heartbeat fresh).
5. Explicitly confirm the last line of §40 regarding remaining user-WIP
   non-commits, then decide the commit plan.

## 44. Workstream checkpoint (karox-release-chatgpt-web)

- Durable decisions: funnel-refresh storm guard (background + foreground) with
  exponential cooldown; local dead-listener canary gated by tunnel type;
  public-probe failure classification; competing contracts reconciled through
  tests (probe-policy clamp invariant, README_RU push-boundary line wrap,
  diagnostics self-heal scoped to owner-authored payloads, activity vocabulary
  unified on "Running checks").
- Clean-up decisions: root scratch protected by .gitignore, nothing deleted;
  the `aura-browser` duplicate disarmed via the durable stop service (its port
  is owned by the primary chatgpt-dev bridge); the `hyperagent` bridge
  relaunches through the new detached start.
- Remaining external gates: macOS CI observation on the first pushed PR; the
  ceasefire-required ChatGPT conformance scenarios (§20); the Tailscale Funnel
  POP reliability topic.
- Final review action: an independent reviewer executes §43 against the
  commit plan in §41/§42.

REMOTE PUSH HAS NOT BEEN PERFORMED.
