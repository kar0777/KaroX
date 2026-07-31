# KaroX 5 installer, migration, rollback, and uninstall rehearsal

Status: required P0 evidence before stable `5.0.0`  
Target runtime: release candidate built from the exact commit under review

This runbook verifies lifecycle behavior on clean Windows, macOS, and Linux
systems. It is not satisfied by running an installer repeatedly on the primary
development machine.

## Safety rules

- Use disposable virtual machines or restorable snapshots.
- Use a disposable Git repository with no credential or production data.
- Preserve the original stable KaroX/RepoPilotBridge installation until the
  rollback window closes.
- Never publish keyring dumps, home-directory archives, or raw support bundles.
- Record exact commands, versions, paths, exit codes, and sanitized diagnostics.
- A failed stage must not be repaired manually before its failure evidence is
  captured.

## Required environments

Record at least:

- Windows 11 with PowerShell 5.1 and a current PowerShell where available;
- current supported macOS on Intel or Apple Silicon represented by CI/release
  support policy;
- one Debian/Ubuntu-family Linux;
- Python 3.10, 3.12, and 3.13 coverage through the remote matrix;
- a system with no prior KaroX installation;
- a system with latest stable 4.x/RepoPilotBridge state;
- a system where Git is initially missing;
- a system where no suitable Python is initially available;
- a headless/keyring-limited Linux environment for fail-closed diagnostics.

## Evidence header

Every rehearsal record includes:

```text
record_id:
verified_at_utc:
karox_version:
karox_commit:
shipping_version:
os:
architecture:
shell:
python_before:
python_after:
git_before:
installation_channel:
result:
evidence_reference:
```

Do not set `result: passed` until all postconditions for that scenario are
checked.

## Scenario A — clean installation

1. Restore a clean VM snapshot.
2. Confirm `karox` is absent.
3. Use the release-candidate local installer, not an unrelated stable bootstrap.
4. Record whether Python and Git are discovered or installed.
5. Open a new shell and run:

   ```bash
   karox --version
   karox paths --json
   karox doctor
   ```

6. Confirm the installed runtime version equals the release candidate.
7. Confirm `karox` and temporary `karox-vnext` compatibility alias resolve to
   the same installed runtime.
8. Confirm no repository outside the selected test repository changed.
9. Confirm no credential is written to plaintext configuration.

Pass conditions:

- installer exits zero;
- launchers work from a new shell;
- installed wheel imports outside the source checkout;
- doctor reports actionable environment limitations;
- no unrelated user data is removed.

## Scenario B — missing dependency diagnostics

Run clean-install attempts with Git absent and with supported Python absent.
Verify one of two honest outcomes:

- the installer safely installs the dependency through the documented platform
  mechanism; or
- it stops with a concrete, localized next action.

A generic stack trace, silent fallback, or partially active runtime fails this
scenario.

## Scenario C — reinstall the same build

Install the same release candidate twice.

Verify:

- the second run is idempotent or clearly performs a safe repair;
- no credential or session is duplicated;
- active launcher target remains valid;
- rollback metadata is not corrupted;
- the test repository is untouched.

## Scenario D — 4.x discovery and dry-run migration

Start from the latest stable 4.x/RepoPilotBridge installation containing
sanitized test configuration and session metadata.

Before KaroX 5 installation, record hashes or directory listings for:

- legacy config;
- legacy application/runtime;
- current KaroX stable runtime;
- selected keyring entries by fingerprint only;
- disposable repository Git state.

Run:

```bash
karox migrate --json
```

Verify:

- command is dry-run without a `--dry-run` flag;
- source files and directories are byte-identical;
- destination is not activated;
- report classifies migratable, skipped, unsupported, failed, and secret
  re-entry items;
- no credential value appears in JSON;
- repository Git state is unchanged.

## Scenario E — applied migration

After reviewing the dry-run report:

```bash
karox migrate --apply --json
```

Verify:

- only the declared migration destination changes;
- writes are atomic;
- unsupported state is reported rather than dropped;
- secrets are resolved through or re-entered into OS keyring namespaces;
- provider, MCP, and bridge credentials remain separated;
- source legacy config, application, and runtime remain present;
- previous stable launcher still starts during the rollback window;
- a KaroX 5 Observe session can inspect the disposable repository;
- a Build session can change a file, run an approved check, and produce Git
  status/diff evidence without commit;
- Advanced can make a guarded local commit in the disposable repository;
- push and publishing remain blocked.

## Scenario F — interrupted staged update

Use the update fault-injection mechanism or a controlled process termination at
each supported stage:

- download;
- extraction;
- staged installation;
- staged validation;
- activation/swap;
- post-activation cleanup.

After every interruption verify:

- either old or new runtime is complete and launchable;
- no half-populated active directory exists;
- previous stable runtime remains available when activation did not complete;
- repository and keyring state remain intact;
- rerunning the installer/update produces a deterministic recovery path.

Do not simulate only network failure; activation and filesystem-lock failure are
required, especially on Windows.

## Scenario G — validation failure and rollback

Deliberately make the staged runtime fail its validation command without
modifying the active runtime.

Verify:

- activation never occurs;
- previous runtime remains selected;
- staged failure is reported with a sanitized reason;
- rollback does not delete legacy or current user data;
- a second attempt after repairing the staged artifact succeeds.

Then test a failure after activation where the updater supports automatic
rollback. Verify the previous runtime is restored atomically.

## Scenario H — active process and file locks

Keep KaroX, a bridge, and representative child processes open while attempting
an update.

Verify:

- updater does not overwrite a running executable in place;
- it either coordinates shutdown, stages for later activation, or stops with an
  actionable diagnostic;
- Windows transient handles are retried only for bounded time;
- a permanently held handle remains a visible failure;
- no partial activation is reported as success.

## Scenario I — uninstall

From a successfully migrated system:

1. Create a disposable KaroX session and test credential.
2. Run the supported uninstall flow.
3. Record every prompt and selected decision.

Verify:

- launchers and selected runtime files are removed;
- user Git repositories are never removed;
- session removal is a separate explicit decision where supported;
- credential removal is a separate explicit decision where supported;
- preserved legacy installation is not silently removed;
- reinstall after uninstall succeeds.

An uninstall script that cannot separate program files from user data must be
fixed before stable release.

## Scenario J — support bundle and diagnostics

Generate the supported diagnostic bundle after one successful and one failed
lifecycle run.

Verify it contains:

- KaroX and platform versions;
- sanitized stage/result metadata;
- paths in redacted or expected form;
- no source file content;
- no API key, bearer token, refresh token, approval password, cookie, private
  key, or keyring value.

## Platform-specific commands

### Windows

```powershell
Get-Command karox -All
karox --version
karox paths --json
karox doctor
python scripts/check_installer_preservation.py --json
```

Capture PowerShell version and installer transcript after redaction. Test paths
with spaces and non-ASCII user names.

### macOS / Linux

```bash
command -v karox
karox --version
karox paths --json
karox doctor
python scripts/check_installer_preservation.py --json
```

Run shell syntax checks and test paths with spaces. On macOS test quarantine or
execution-policy behavior applicable to the distributed artifact. On Linux test
a headless keyring failure path.

## Completion gate

Stable 5.0 requires a passed sanitized record for every scenario on each relevant
OS family. Aggregate results should be linked from `docs/RELEASE_CHECKLIST.md`.

A lifecycle scenario remains failed when the maintainer had to delete a runtime
directory, legacy directory, `.git`, keyring store, or user configuration by hand
to continue.
