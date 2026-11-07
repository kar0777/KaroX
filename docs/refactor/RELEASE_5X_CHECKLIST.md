# Release 5.x checklist

**No release number has been committed yet.** The tree still reports
`6.0.0a3`; changing only that string is forbidden.

## Version decision evidence

- PyPI `karox-runtime` visible 5.x releases: `5.0.0rc2`, `5.0.0rc3`;
  no stable `5.0.0` observed.
- Local v5 tags: `v5.0.0rc1/rc2/rc3`; no `v5.0.0`.
- Local v5 runtime source is `5.0.0rc4`, but its release gate still says
  NOT READY and its shipping `VERSION` file is 4.1.4.
- Target is a clean 6.0.0a3 line with deliberate exclusions from v5 laboratory
  scope. Calling it `5.0.0` without restoring/declaring the v5 shipping scope
  would be misleading.
- Candidate free numbers considered: `5.0.0` and `5.1.0`. The version decision
  is blocked on scope and migration, not on availability.

## P0 before a local 5.x candidate

| Item | Status |
|---|---|
| Version/classifier/PEP 440 consistency | NOT DONE |
| README, README_RU, CHANGELOG, INSTALL, migration text | NOT DONE |
| 5.x upgrade/rollback and `git_baseline` session compatibility | **BLOCKER**: target adds a field to schema 1; rc4 rejects unknown fields; rollback needs restore |
| Installer detect/upgrade/alias behavior | **BLOCKER**: current installers are 6 side-by-side, default stale `v6/...` |
| Preserve/port `karox-vnext`/`karox-agent-mcp` aliases | **DECISION REQUIRED**; absent in target |
| Existing v5 lab capabilities (orchestration/subagents/checkpoints/connections) | **SCOPE BLOCKER** unless honestly excluded by owner |
| Wheel build from final HEAD | PASS — `2eed6bfb...` 1,150,836 bytes |
| Fresh venv install, CLI/TUI smoke | PASS — version/paths/agent help exit 0 |
| Wheel package-data/license inspection | baseline passed; final pending |
| Full one-revision pytest | **FILE-ISOLATED PASS; SINGLE-PROCESS NOT GREEN** (1,525P/15S/0F/0H across 70 files) |
| Real provider/browser/MCP/TUI smoke | NOT RUN |
| Benchmarks | NOT RUN |

## Safe migration rehearsal (not executed on the user's install)

1. Stop TUI/agent/bridges; do not touch Tailscale routes or keyring.
2. Back up `%APPDATA%\KaroX` and `%LOCALAPPDATA%\KaroX\vnext` plus
   `web-bridge`, `runtime-restarts`, `saved-bridge-*`, `tailscale-bin`.
3. Validate the wheel in a fresh venv with isolated config/runtime.
4. Rehearse old state on a **copy**; check `session list`, `bridge saved list`,
   `provider list`, `mcp server list`, `doctor`.
5. Upgrade the canonical `karox` command only with explicit owner approval.
6. Rollback requires restoring the session/runtime copy because schema-1
   `git_baseline` is not readable by rc4.

## Local artifact policy

No push, tag, PyPI/GitHub release, deployment, or replacement of the installed
Karo command without a separate exact owner approval. The final candidate is a
local wheel + SHA-256 + evidence bundle only.

## Required final receipt

- version + PEP 440 wheel name;
- wheel SHA-256 and contents;
- Git HEAD/commits and clean worktrees;
- before/after physical/nonempty/comment LOC;
- removed/kept dead code;
- full gate logs;
- migration/rollback result;
- benchmark ledger and limitations;
- exact NOT_READY/READY verdict.
