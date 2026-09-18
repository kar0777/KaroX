# Migrating from KaroX 4.x to KaroX 5

Status: release-candidate migration contract for `5.0.0rc2`.

KaroX 5 introduces the packaged hybrid runtime under `src/karox`. The stable
4.x product and its legacy bridge remain present while the 5.0 migration is
validated. Do not delete a working 4.x installation merely because the preview
runtime starts successfully.

## Guarantees

Migration must:

- default to a dry run;
- leave the source installation untouched;
- never modify a user repository, branch, commit, remote, or working tree;
- never run Git push or package publishing;
- copy only validated non-secret metadata;
- move secrets directly into the operating-system keyring when the source can
  be read safely, otherwise require secure re-entry;
- produce a JSON report containing migrated, skipped, unsupported, and failed
  items;
- use atomic destination writes and roll back partial output;
- preserve a way to launch the previous stable runtime until 5.0 rollback gates
  have passed.

Migration rollback restores KaroX configuration and launcher selection. It does
not attempt to reverse repository edits made by an agent.

## Before migrating

1. Confirm the current stable version with the existing 4.x CLI.
2. Stop active KaroX sessions and tunnels.
3. Run the existing product doctor and save a sanitized support bundle when a
   problem is already present.
4. Commit or otherwise back up important repository work. Migration itself does
   not edit repositories, but an unrelated uncommitted working tree is harder to
   diagnose later.
5. Do not copy API keys, bearer tokens, or session secrets into notes or issue
   reports.

## Preview migration flow

Discover legacy data without changing it. Dry-run is the default; there is no
separate `--dry-run` flag:

```bash
karox migrate --json
```

Use explicit source or destination paths only when the automatic legacy and
migration locations are not the intended ones:

```bash
karox migrate --legacy-config PATH --destination PATH --json
```

Review the report. It must name every source location and classify each item as
migratable, skipped, unsupported, or requiring secret re-entry.

Apply the migration only after the report is understood:

```bash
karox migrate --apply --json
```

The public parser contract is:

```text
karox migrate [--legacy-config PATH] [--destination PATH] [--apply] [--json]
```

Documentation must not invent an option that the runtime does not implement.

## Data handling

### Provider and model metadata

Provider names, endpoint metadata, model IDs, capability declarations, privacy
classes, and pricing provenance may be imported after schema validation.
Credentials must not be embedded in imported headers, query parameters, or
configuration values.

### Credentials

KaroX 5 uses separate OS-keyring namespaces for provider, MCP, and bridge
credentials. Plaintext fallback is disabled. Environment-only values are not
copied into persistent configuration; the migration report asks the user to
re-enter them through the credential command or setup UI.

### Sessions

Compatible session state is converted into versioned, checksum-protected KaroX
5 records. Repository identity is revalidated. Unsupported or ambiguous state
is reported instead of being silently dropped. Full chat transcripts are not a
migration requirement; the structured goal, constraints, decisions, changed
files, checks, evidence, Git state, and unfinished work are the durable state.

### Legacy bridges

The Notion gateway and other legacy paths remain available during the dual-
runtime period. They are not automatically relabelled as KaroX 5 live-tested
integrations. Product status follows the evidence rules in
`docs/V5_RELEASE_SCOPE.md` and `docs/conformance/`.

## Upgrade verification

After migration:

1. Run `karox doctor`.
2. Confirm the selected repository and access profile before granting mutation.
3. Confirm provider and bridge credentials resolve from the expected keyring
   namespace without being printed.
4. Start an Observe session and read Git status.
5. Start a disposable Build session in a test repository.
6. Make one small change, run one approved check, inspect Git diff, and restart
   the session.
7. Confirm the completed mutation was not replayed.
8. Confirm the old stable launcher still works until the release notes say the
   rollback window is closed.

## Rollback

A failed KaroX 5 preview must not require deleting user data. Use the installer
or launcher rollback path documented by the release candidate, then run the 4.x
product doctor. Keep the KaroX 5 migration report for diagnosis, but redact it
before sharing.

Never solve a migration failure by deleting a repository, `.git`, OS keyring,
or the complete user runtime directory without first identifying which data is
safe to remove.

## Retirement of the legacy runtime

Legacy code may be removed only after:

- clean install, migration, interrupted update, rollback, and uninstall gates
  pass on Windows, macOS, and Linux;
- two consecutive release candidates complete the migration rehearsal;
- no required live integration depends on the legacy server;
- release notes identify every removed command and replacement;
- external beta testers successfully migrate without maintainer intervention.

Until then, legacy code is compatibility debt, not permission to extend two
independent products indefinitely.
