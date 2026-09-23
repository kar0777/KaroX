# Changelog

All notable changes to KaroX 5 are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — 2026-09-23

- Compact tool discovery now accounts for its own prompt overhead. Deferring a
  small schema never increases the request size, and usage reports show net savings.
- Repeated reads with equivalent JSON object arguments share stale-result
  identity; non-finite, malformed and non-object arguments remain distinct.
- Repository inspection favours relevant primary source over keyword-heavy
  snapshot paths and coalesces overlapping excerpts within the original budget.
- The dark terminal shell uses more of narrow viewports. Its command palette
  keeps the composer visible at 46x14 and scrolls through the full keyboard list.
- Separate StepFun API and Step Plan presets prevent mixing pay-as-you-go and
  subscription endpoints. HTTP 402 is classified as a budget error.
- Added an opt-in, read-only Step 5 Preview acceptance runner with independent
  review/test-design lanes, strict answer/read evidence, source fingerprints and
  credential-safe failure reports.
- A typed command is no longer consumed as pasted text. Enter arriving inside the
  machine-speed burst that arms the raw-paste detector used to be stored as a
  pasted newline, so `/connect` never ran and the composer showed a paste marker;
  the command palette now outranks that reading. The palette's own window also
  fits the rows its border leaves paintable, so a long catalogue can no longer
  place the highlighted command in a clipped row.
- The provider registry read and its atomic swap pass through the Windows sharing
  window instead of failing: a status read that lands inside the swap, or a save
  whose swap meets an open reader, is retried on a bounded budget. This fixes the
  two Windows Python 3.12 CI failures at their cause rather than in the tests
  that tripped over them.
- An HTTP 402 is a budget error on both delivery paths, and it falls back to the
  next configured route: an exhausted allowance belongs to that endpoint's
  account, while the local budget stop is raised before any route is tried.
- Repository excerpts can no longer be truncated into hiding the match line that
  created them, deeply nested tool arguments can no longer end a run with a
  RecursionError, and the deferred-tools row says when a step deliberately
  skipped deferral instead of reporting a measured zero.
- Tool calls work on the StepFun Step Plan endpoint. It streams one call as a
  first fragment carrying id/type/name and later fragments carrying `"id": ""`
  and `"type": ""` with only the argument text appended; the empty string was
  read as a foreign tool-call type, so every tool call on that endpoint failed
  with `malformed_response`. A foreign type is still rejected.
- `karox orchestrate run` no longer aborts before its first worker: a
  subscription worker publishes its check evidence under a context kind the bus
  vocabulary did not list.

## [5.0.0rc2] — 2026-09-18

Release-gate correction candidate. Full notes: `RELEASE_NOTES_v5.0.0rc2.md`.

### Release pipeline hardening
- Fixed the stale installer/update contract so CI validates the current package-metadata dependency path instead of the removed requirements/no-deps path.
- Coverage CI now installs the checkout itself, so direct imports and the real `karox` console entry point are available under coverage.
- Installed-wheel matrix tests explicitly switch back to repository `src` for the source-suite phase after independently proving the installed wheel.
- Windows root help is cp1252-safe, avoiding a Unicode-arrow crash before argument parsing.
- Tailscale supervisor tests use impossible synthetic child PIDs so a hosted POSIX runner can never mistake a fake PID for a live process group.
- `v5.0.0rc1` reached the Git tag gate but stopped before PyPI/GitHub Release publication; `rc2` supersedes it for public beta distribution.

## [5.0.0rc1] — 2026-09-17

First public release candidate. Full notes: `RELEASE_NOTES_v5.0.0rc1.md`.

### Release candidate preparation
- **Version**: runtime `5.0.0.dev0` → `5.0.0rc1` (`src/karox/__init__.py`,
  `remote/src/karox_remote/__init__.py`); every document, conformance record
  and template that quotes the runtime version updated with it.
- **Distribution**: the live beta remains installable directly from the preview
  branch; `.github/workflows/prerelease.yml` validates the exact candidate,
  smoke-tests the built wheel, then publishes matching pre-release tags to PyPI
  through trusted publishing and creates a GitHub pre-release. The stable
  `release.yml` path remains separate; `bootstrap.sh` / `bootstrap.ps1` accept
  `--channel preview`.
- **`karox quickstart`**: one-screen onboarding that reports the resolved
  repository, language, connected providers and bridges, and the single next
  command to run — no bridge vocabulary on the first screen.
- **Repository hygiene**: 21 historical `RELEASE_NOTES_v3.*/v4.*` moved to
  `docs/releases/`; eight superseded planning documents moved to
  `docs/archive/` with an index that names what replaced each one; stray
  benchmark artefacts moved out of the root.
- **Documentation truth**: `docs/IMPLEMENTATION_STATUS.md` rewritten to the
  current tree (memory, project map, orchestration, browser, multi-project
  bridge were missing); published test counts republished from discovery
  (3325 suite / 3330 root); `RELEASE_CHECKLIST.md` section 4 now names the test
  module behind each security claim.
- **Terminal client**: UX-010 closed — `test_a_narrow_window_gives_the_chat_a_usable_share`
  holds the conversation at four rows in a 14-row window. UX-006 remains open:
  no binding hands the mouse back to the terminal emulator, and no test asserts
  one. Tracked in `docs/UX_BUG_INVENTORY.md`.
- **Coverage**: measured 72.4% statement+branch. The gate stays at 70 in
  `pyproject.toml`, deliberately, to leave room for platform-skipped branches on
  the macOS and Linux runners.
- **CI**: install → `karox --help` → `karox migrate --json` → uninstall
  rehearsal job on Windows, macOS and Linux with logs kept as artefacts.

### Total product QA + UX hardening (2026-08-21)
- **Shared bridge multi-project fix**: selecting another approved project no
  longer degrades the bridge card to "not configured / Launch KaroX".
  `ServiceConnectScreen` discovery now honours the saved profile's approved
  project registry; unapproved projects still never match.
- **Small-terminal usability**: `ServiceConnectScreen`, `ConnectionHubScreen`,
  `ModelProvidersScreen` (also fixed a hard `width: 86`), and the Ctrl+W
  workspace manager scroll instead of clipping controls; focus scrolls into
  view. New gate `tests/test_tui_total_size_acceptance.py` exercises
  40x12..160x45 plus 73x19, RU at every size.
- **Coverage stall hardening**: the shared test git sandbox isolates
  GIT_CONFIG_GLOBAL/SYSTEM, disables prompts, and bounds `git init` at 120s
  with an actionable error, removing the fsmonitor/locked-gitconfig hang mode
  observed during the owner's coverage run.
- **Total UI acceptance record**: docs/V5_TOTAL_UI_ACCEPTANCE.md inventories
  all 35 shipping screens with acceptance state and evidence.
- Published test counts republished from discovery: 2775 suite / 2780 root.

### Hard-close: adaptive output wired at the production call-site (2026-08-21)
- **P1.4 production wiring**: `karox agent run` now narrates every finished
  native-agent turn through `karox.output_policy` — new `--output-mode`
  flag (adaptive default), `AgentReport` folded into a `TurnReport`, the
  complete legacy field dump preserved one mode away (DETAILED) and one
  flag away (`--json`). Failures, warnings, compaction notices, and check
  failures survive every mode by contract.
- **Wiring acceptance**: `tests/test_output_policy_wiring.py` proves the
  production path (`cli._print_agent_report`) renders through the policy
  and that the parser exposes `--output-mode`; unwiring the policy fails
  this suite, not a unit test of an unused class.
- Published test counts republished from discovery: 2769 suite / 2774 root.

### Release pass 3 live continuation (2026-08-20, evening)
- **Unicode/Cyrillic memory retrieval**: NFKC + casefold normalization with
  ё→е collapse, snake_case key parts as tokens, and a small deterministic
  multilingual alias map (name/preference/user concepts) with exact-key and
  key-overlap boosts. "Как меня зовут?", "МОЁ ИМЯ?", "what is my name?" and
  mixed-language queries now retrieve the same USER memory under the same
  retrieval budget. Russian/mixed regression suite added.
- **Stale-client catalog handling**: the stateless MCP wire cannot deliver
  `notifications/tools/list_changed`, so a call naming a tool outside the
  current catalog now answers with the one correct fix -- reconnect this
  client (same bridge, same credential, same URL) -- and, when the cached
  catalog is known, the exact count of newly available tools. Regressions
  cover the schema-snapshot upgrade path and both tool-name spellings.
- **P1.5 tool catalog economy**: deterministic core/task/memory/browser/
  devserver/admin groups as a pure function of the tool name, exposed under
  `tool_catalog.groups` in bridge diagnostics; equal catalogs render
  byte-identical payloads.
- **P1.4 adaptive/concise output policy** (module): TurnReport renderer with
  adaptive/concise/standard/detailed/learning modes and progressive
  disclosure; failures and warnings survive every mode by contract; measured
  >40% character reduction on verbose green reports.
- **Release contract machine-side**: `scripts/check_v5_release.py` now runs
  in-process through pytest (`tests/test_v5_release_contract.py`) on every
  approved test surface.

### Release pass 3: Universal memory, project intelligence, quality economy (2026-08-20)
- **Universal memory layer** (`karox.memory`): USER/PROJECT/WORKSTREAM/SESSION
  scoped local store — inspectable, forgettable, credential-shaped content
  refused, keyed upserts, TTL expiry, deterministic budgeted recall,
  source-hash revalidation.
- **Memory protocol for every client**: `karox.memory.remember/recall/context/
  list/forget` served by the autonomy runtime and registered in the default
  web bundle, CLI bundle completion, TUI read family, and capability
  negotiation. Cross-client recall proven without shared transcripts.
- **Project fact map**: deterministic local onboarding facts with evidence
  hashes (project type, build/test commands, package manager, entrypoints,
  languages) plus AGENTS.md/CLAUDE.md/.cursorrules ingestion and incremental
  refresh; `karox.task.bootstrap` now returns the compact digest.
- **Quality economy wired, not asserted**: the native agent turn now drives
  StablePrefixCache (prefix-hash-aware provider cache keys),
  ToolSchemaDeduplicator (advertised vs unique schema bytes), ReadCache
  (repeated unchanged reads), CostLedger (every round trip) and a
  shadow-mode CostGovernor; `task.execute_plan` reports counted
  round-trips-avoided batch economy. Acceptance tests assert the production
  paths invoke the stack.

### Phase 0: Safe Bridge Recovery
- **Secret-safe credential rotation**: `karox bridge credential rotate-key`
  no longer prints the secret to stdout. New `--copy` flag delivers
  `Bearer <secret>` to the clipboard with 120-second auto-clear.
- **Port ownership**: `karox bridge status/stop/restart/attach --saved NAME`
  classify who holds the port and only act on proven KaroX-owned processes.
- **Tailscale route ownership**: individual route inventory and classification
  (reuse/stale/foreign). Stale owned routes are cleared safely; foreign routes
  are never touched.
- **Honest tunnel**: default tunnel changed from `cloudflare` to `tailscale`
  (cloudflared is absent on the target host).

### Phase 1: ClickUp + ChatGPT Auth
- **ClickUp**: connected via Bearer header (Authorization: Bearer <secret>).
- **ChatGPT**: connected via OAuth PKCE S256 (full authorization server with
  DCR, refresh rotation, revocation).
- **OAuth approval-password fix**: launcher no longer prints the approval
  password at startup. New CLI command `karox bridge oauth approval-password
  --copy` resolves the current value at copy time.

### Phase 2: Saved Profile UX
- Browser policy flags added to `bridge saved edit` (--browser-headed,
  --no-browser-headed, --browser-domain, --browser-deny-domain, etc.).
- `/sessions` opens the compact Session Browser by default; `--verbose` or
  `/session-log` for the text renderer.

### Phase 3: Typed Process-Boundary Transcript
- SQLite WAL transcript store with 19 typed event types, idempotent append,
  crash-safe replay, and secret redaction at the process boundary.
- Shadow-mode parity verified on 5 real agent workloads (read-only analysis,
  multi-file edit, tool failure + retry, restart + replay, verification fix).
- `_poll_agent_history` removed and replaced with typed transcript reader
  (`_poll_typed_transcript`). Assistant content still read from session store.

### Phase 4: Verification Autopilot
- Typed `VerificationResult` with explicit acceptance rules (exit 0 ≠ success
  for non-test commands; test runners require exit 0 + pass evidence).
- Autopilot layer selection: focused → related regression → broader → release
  gates.

### Phase 5: Cost Intelligence (CI-0 through CI-5)
- CI-0: UsageRecord + CostLedger (accepted-verified-task-cost metric).
- CI-1: StablePrefixCache, ToolSchemaDeduplicator, ReadCache.
- CI-2: CompactSummary (typed, content hash, raw artifact, redacted).
- CI-3: ContextWindow (pinned facts, bounded ring, artifact store).
- CI-4: BatchPlanner (parallel reads, sequential writes, verification bundling).
- CI-5: CostGovernor (soft/hard budgets, user override, shadow mode).

### Phase 6: Smart Stop
- No-progress detection: repeated tool calls, verification failure loops,
  budget exceeded, unchanged workspace. Never stops useful long builds or
  confirmation-waiting states.

### Phase 7: Plan / Act
- Explicit mode transition with audit trail. Starts in Plan (read-only);
  transition to Act requires reason and is logged.

### Phase 8: Checkpoints
- Workspace mutation transaction model: before/after hash, reversible flag,
  undo preview.

### Phase 9: Packaging
- Test count updated to 1864 (suite) / 1869 (root).
- Release gates: 10 OK. Wheel contents: 9 OK.
- `scratch/` and `.claude/` excluded from Git and wheel.
