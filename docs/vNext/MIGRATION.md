# Migration to KaroX vNext

## Strategy

Migration is incremental and reversible. vNext installs beside the current
runtime, reads legacy state through explicit importers, and keeps the verified
Notion bridge available. Removal of a legacy path requires equivalent behavior,
tests, migration diagnostics, and a rollback path.

## Baseline inventory

| Legacy area | Decision |
| --- | --- |
| Repository confinement, secret scanning, no-push/no-publish rules | Preserve behavior; reimplement behind Core policy with regression tests |
| File/Git/process/browser/desktop tools | Adapt, then split into Core executors |
| Audit and persisted session data | Migrate to versioned session/evidence records |
| Notion gateway/profile/provider | Preserve unchanged until equivalent vNext E2E exists |
| PromptQL launcher/OpenAPI path | Keep available and label experimental |
| HyperAgent claims | Label experimental until a reproducible E2E exists |
| `server/repo_tools.py` global state | Contain in legacy adapter; do not extend |
| Large PowerShell/POSIX launch scripts | Keep for stable install, move new logic into Python CLI |
| Script-style tests | Keep in CI; add a discoverable vNext test suite |

## Configuration migration

The migration command will:

1. Discover legacy runtime homes without modifying them.
2. Validate paths, versions, and permissions.
3. Copy only non-secret provider/model/profile metadata.
4. Import secrets directly into the OS credential store when their source is
   verifiably protected; otherwise require secure re-entry.
5. Convert session state to a versioned schema and write an import report.
6. Keep the source untouched and record a rollback marker.

Plaintext secrets are never copied to vNext configuration. Environment-only
keys cannot be migrated automatically; the report explains how to re-enter
them. Unknown fields are retained in a redacted diagnostic section, not silently
dropped.

## Compatibility phases

- **Bridge:** vNext CLI can start/inspect the existing server and Notion path.
- **Dual runtime:** native work uses vNext Core while hosted clients may use the
  legacy adapter; both register session origin and repository identity.
- **Native bridge:** verified clients move to the shared vNext MCP server.
- **Retirement:** old code is removed only after cross-platform and migration
  gates pass for two consecutive release candidates.

## User-visible guarantees

- Migration never pushes, publishes, or changes the checked-out branch.
- A dry-run and JSON report are available.
- Existing state is backed up before any in-place launcher update.
- Unsupported data produces a specific warning and remediation, not a success.
- Rollback restores launch selection; it does not pretend to reverse repository
  edits made by agents.

## Current risks

- Import-time globals make parallel legacy sessions difficult to isolate.
- Runtime extensions mutate `repo_tools` globals and need containment tests.
- Windows and POSIX launchers duplicate substantial behavior.
- Existing session formats may not contain sufficient structured handoff data.
- Working Notion tests log noisy upstream shutdown exceptions despite passing.

No legacy code has been removed in Phase 0.

## Implementation status (Phases 0-10)

This strategy is now backed by implemented code on the
`codex/vnext-hybrid-runtime` branch:

- **Legacy discovery/import** is implemented: `karox migrate` discovers legacy
  runtime homes without modifying them, validates paths/versions/permissions,
  copies only non-secret metadata, imports secrets into the OS credential
  store, converts session state to a versioned schema, writes a JSON/dry-run
  report, and keeps the source untouched. Covered by `test_migration_cli.py`.
- **Credential isolation** is implemented: provider, MCP, and bridge secrets
  live in dedicated keyring namespaces (`KaroX/provider`, `KaroX/mcp`,
  `KaroX/bridge`) and never in plaintext config. Covered by
  `test_credentials.py` and `test_bridge.py`.
- **Session versioning** is implemented: durable, checksum-protected session
  records with mutation leases, fencing tokens, and idempotency. Covered by
  `test_sessions.py` and `test_handoff.py`.
- **Notion bridge** remains available and `TESTED` via the bundled transport
  regression (`scripts/test_notion_mcp_transport.py`, KB-HYBRID-08).
- **PromptQL and HyperAgent** remain labelled `EXPERIMENTAL` (no dedicated
  product E2E on this branch).

No legacy code has been removed. vNext installs beside the current runtime; a
retirement phase (removal of legacy paths) is explicitly **not** part of this
branch's work and would require cross-platform and migration gates to pass for
two consecutive release candidates, per the compatibility phases above.
