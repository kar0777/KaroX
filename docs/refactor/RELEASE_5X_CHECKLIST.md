# Release 5.x checklist

**Release number: 5.0.0**, chosen on branch `release/karox5-5.0.0`. The tree,
wheel and console now report `5.0.0`; changing only the string was not enough —
README/RU, INSTALL, CHANGELOG, PARITY, MIGRATION, wheel and fresh-install smoke
were updated and re-run.

## Version decision evidence

- PyPI `karox-runtime` visible 5.x releases: `5.0.0rc2`, `5.0.0rc3`;
  no stable `5.0.0` observed.
- Local v5 tags: `v5.0.0rc1/rc2/rc3`; no `v5.0.0`.
- Local v5 runtime source is `5.0.0rc4`; the new artifact is the stable
  clean-line continuation, not a republished v5 laboratory.
- `5.0.0` is the canonical fifth-line final; `5.1.0` remains free for a later
  minor. The old lab's orchestration/autonomy surfaces are explicitly out of
  the stable scope (documented in PARITY/MIGRATION), not silently omitted.

## P0 before a local 5.x candidate

| Item | Status |
|---|---|
| Version/classifier/PEP 440 consistency | **PASS** — `karox 5.0.0`, wheel metadata `karox_runtime-5.0.0` |
| README, README_RU, CHANGELOG, INSTALL, migration text | **PASS** for the 5.0.0 local candidate |
| 5.x upgrade/rollback and `git_baseline` session compatibility | **PARTIAL/PASS for restore path**: rc4 old session read by 5.0.0; new session rejected by rc4 (`unknown session fields: ['git_baseline']`); restore of timestamped config/runtime backup lets rc4 read old session again. In-place downgrade unsupported. |
| Installer detect/upgrade/alias behavior | **PASS local**: 5.0.0 installer requires explicit spec, backs up with `KAROX_UPGRADE=1`, uses `pipx install --force`, leaves `karox-runtime6`; PowerShell 9 tests pass; POSIX fresh/refuse/upgrade/backup scenario passed in WSL Ubuntu. Published URL still NOT TESTED. |
| Preserve/port aliases | `karox-vnext` restored; `karox-agent-mcp` deliberately out of stable scope |
| Existing v5 lab capabilities | **DELIBERATE EXCLUSION**, documented; not claimed as stable features |
| Wheel build from final HEAD | **PASS** — `d39da0f0...`, 1,150,912 bytes |
| Fresh venv install, CLI/TUI smoke | **PASS** — `karox` and `karox-vnext` both report 5.0.0; paths/agent help exit 0 |
| Wheel package-data/license inspection | PASS for prior wheel; final package-data smoke PASS |
| Full one-revision pytest | **PASS** — 5.0.0 single process exit 0, 1,542 collected; file-isolated 1,525P/15S/0F |
| Real provider/browser/MCP/TUI smoke | **real browser scenario PASS** (verified, 3 steps, 16,857 tokens, open/get_text); provider smoke PASS; real TUI/bridge scenario still partial |
| Benchmarks | **PILOT ONLY**: one paired baseline/candidate cell; OpenCode 2.2.1 free-model product pass; DSH blocked by missing credential. No 30–100-task representative matrix. |

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


