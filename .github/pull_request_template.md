## Scope class

Choose one and explain why:

- [ ] P0 — release blocker
- [ ] P1 — release quality
- [ ] P2 — post-release
- [ ] Deferred / scope change requested

Scope rationale:

## User problem and primary flow

What user-visible problem does this solve? Which step of the KaroX 5 primary
install → connect → mutate → verify → diff/evidence → resume flow changes?

## Security and trust boundary

- Origin/capability affected:
- Repository/path boundary affected:
- Credential namespace or OAuth flow affected:
- Mutation lease/idempotency affected:
- Process or external MCP authority affected:
- Push/publish/destructive-action boundary affected:

- [ ] The change does not describe KaroX as an operating-system sandbox.
- [ ] Hosted authentication is not treated as a capability grant.
- [ ] No secret value reaches config, logs, session state, evidence, MCP
      descriptors, support bundles, or public fixtures.

## Implementation

Summarize the changed public commands, files, schemas, migration behavior, and
rollback behavior.

## Evidence actually run

Focused checks:

```text
commands and results
```

Repository gates:

- [ ] `python scripts/check_dependencies.py`
- [ ] `python scripts/check_versions.py`
- [ ] `python scripts/check_test_count.py`
- [ ] `python scripts/check_v5_release.py --json`
- [ ] `python scripts/check_release_workflow.py`
- [ ] `python -m ruff check src tests scripts`
- [ ] `python -m mypy src/karox`
- [ ] `python -m unittest discover -s tests -p "test_*.py"`
- [ ] Coverage remained at or above the configured threshold
- [ ] Wheel built, installed, and imported outside the source tree

Platforms actually exercised:

- [ ] Windows
- [ ] macOS
- [ ] Linux

Do not check a platform that was only covered by an assumed or pending CI job.

## External conformance

- [ ] No third-party compatibility claim changed
- [ ] Contract tested locally only
- [ ] Live tested and dated record updated under `docs/conformance/`

External product/account/model and evidence reference:

## Documentation

- [ ] English and Russian user instructions remain behaviorally equivalent
- [ ] Canonical KaroX 5 docs were updated
- [ ] Security boundary changes are reflected in `SECURITY.md`
- [ ] Migration/update behavior is reflected in `docs/MIGRATION_V4_TO_V5.md`
- [ ] No new shipping documentation was added under archived `docs/vNext/`

## Known limitations and remaining work

State what was not tested or intentionally deferred. Do not use “all tests pass”
without naming the command and current tree on which it ran.
