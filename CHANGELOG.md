# Changelog

All notable changes to KaroX 5 are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [5.0.0-dev] — Unreleased

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
