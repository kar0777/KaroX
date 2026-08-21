# Changelog

All notable changes to KaroX 5 are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [5.0.0-dev] — Unreleased

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
