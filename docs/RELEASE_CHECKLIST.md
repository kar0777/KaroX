# KaroX 5 release checklist

Target: `5.0.0`
Current runtime: `5.0.0rc2`
Release decision: **RELEASE CANDIDATE HARDENING — deterministic gates must be green before promotion.**

This is an execution record, not a marketing plan. A checked P0 item must point
to reproducible evidence. Code presence is not evidence that a command, platform,
or external product passed.

## 1. Scope and feature freeze

- [x] Product promise and stable scope are frozen in `docs/V5_RELEASE_SCOPE.md`.
- [x] Shipping, Preview/Legacy, and deferred features are separated.
- [x] Status vocabulary distinguishes unit, contract, and live evidence.
- [x] P0/P1/P2/deferred classification is required by the PR template.
- [x] CONTRIBUTING documents the 5.0 feature-freeze rule.
- [ ] Every currently open pull request is classified.
- [ ] No deferred feature is merged without an explicit scope change.

Evidence:

- `docs/V5_RELEASE_SCOPE.md`
- `CONTRIBUTING.md`
- `.github/pull_request_template.md`

## 2. Product and documentation consistency

- [x] Runtime version source is `src/karox/__init__.py`.
- [x] Wheel version derives from `karox.__version__`.
- [x] Stable shipping line remains tracked separately by `VERSION`.
- [x] `scripts/check_v5_release.py` blocks an incomplete final `5.0.0`.
- [x] `scripts/check_access_profiles.py` reads the real policy table.
- [x] `scripts/check_versions.py` invokes product, profile, and workflow contracts.
- [x] English and Russian README describe KaroX 5 Preview.
- [x] Quick start covers ChatGPT, Claude, API providers, verification, and resume.
- [x] Public migration examples match the actual parser: dry-run is the default;
  only `--apply` writes.
- [x] Observe/Browser/Build/Advanced documentation matches
  `read_only/browser_control/workspace_write/elevated`.
- [x] Build is documented without `git.commit`; Advanced owns guarded local
  commit; no stable profile owns push or publish.
- [x] `docs/vNext/README.md` is marked historical and points to canonical pages.
- [x] Security and troubleshooting state the OS-sandbox limitation.
- [x] Ellipsis local-agent documentation states that the selected local Git root
  remains the only working tree and that no cloud clone is used.
- [ ] Every remaining shipping command in every public document is exercised
  against the installed wheel's `--help` output.
- [ ] No stale user-facing `vNext` help text remains in the installed CLI/TUI.

Evidence commands:

```bash
python scripts/check_dependencies.py
python scripts/check_versions.py
python scripts/check_test_count.py
python scripts/check_v5_release.py --json
python scripts/check_access_profiles.py --json
python scripts/check_release_workflow.py --json
```

## 3. Current ChatGPT/Claude bridge copy fix — done

The runtime source used to send a user to `Settings → Plugins`, a ChatGPT path
that no longer exists; the current product uses Apps with developer mode. The fix
is applied, tested, and enforced, so this section is closed rather than pending.

- [x] Official ChatGPT and Claude setup paths were reviewed on 2026-07-29.
- [x] `docs/LIVE_TEST_RUNBOOK.md` records current external requirements.
- [x] `src/karox/web_bridge_launcher.py` names `Settings → Apps` and
  `Apps → Create` in English, `Настройки → Приложения` and `Приложения → Создать`
  in Russian, and the Claude path as
  `Settings → Connectors → Add custom connector`.
- [x] No ChatGPT instruction contains `Plugins` or `Плагины`;
  `scripts/check_v5_release.py` fails the release if either returns, and
  `tests/test_web_bridge_launcher.py::WebBridgeInstructionTests` asserts the
  current wording in both languages.
- [x] The one-time helper was applied and deleted. `scripts/check_release_hygiene.py`
  and `scripts/check_v5_release.py --strict` both refuse a shipping tree that
  contains any `scripts/apply_v5_*.py`.

Verification, which needs no helper:

```bash
python scripts/check_v5_release.py --json
python -m unittest tests.test_oauth_bridge tests.test_web_bridge_launcher -v
git diff --check
```

## 4. Core correctness and security

- [ ] Complete 3526-test suite passes on the final release commit; current
  verification is pending. The following is historical evidence, not current
  acceptance: the September 17 beta validation passed. The stale
  2026-09-03 failing-run narration above has been superseded: the CLI surface,
  developer runtime, desktop capture, TUI, agent verification, and release-gate
  findings were repaired across subsequent commits. Re-verified 2026-09-17 on
  Windows: canonical `unittest` completed `OK (skipped=6)` and a full xdist run
  completed with 4007 passed, 9 skipped, and 2082 subtests. Ruff, Mypy, wheel
  build + contents + installed-wheel smoke, the non-strict v5 beta gate, access
  profile gate, release hygiene, and `git diff --check` are also green.

- [x] `python scripts/check_wheel_contents.py` passes on the built wheel, after
  deleting `build/` so no removed module can ship from a stale copy.

- [x] Ruff passes without suppressing new defects. Re-verified 2026-09-17 on the
  working tree: exit 0.

- [x] Mypy passes. Re-verified 2026-09-17: 191 source files, no issues.
- [x] Coverage passes without lowering the configured threshold (72.4195% vs 70%).
- [x] KB-HYBRID-01..10 pass; the 2026-09-17 canonical run regenerated all ten
  successful gate records.
- [ ] Traversal tests pass.
- [ ] Symlink and Windows reparse-point escape tests pass.
- [ ] Sensitive-path and secret-reflection tests pass.
- [ ] Hosted tool allowlist tests pass.
- [ ] External MCP schema/identity drift tests pass.
- [ ] Concurrent mutation is blocked.
- [ ] Stale lease fencing is verified.
- [ ] Idempotent replay returns the recorded result.
- [ ] Unknown mutation outcome is not reported as success.
- [ ] Failed or timed-out verification cannot produce verified success.
- [ ] Build cannot create a local commit.
- [ ] Advanced can create only a guarded local commit.
- [ ] No shipping profile grants standing Git-push authority; the dedicated
  hosted `karox.git.push` path requires a fresh exact-action MCP user approval,
  and force-push cannot be expressed through the shipping tool surface.
- [ ] Package publishing remains blocked through every shipping profile.
- [x] Support bundle contains no source or credential in the adversarial local gate.

Evidence:

- Local Windows acceptance: `docs/evidence/local-autonomy-acceptance-2026-08-07.md`
- Beta HEAD verification 2026-09-17: canonical `python -m unittest discover -s tests -p "test_*.py"`
  completed `OK (skipped=6)`; full xdist completed 4007 passed, 9 skipped and
  2082 subtests; KB-HYBRID-01..10 passed 10/10; Ruff/Mypy clean; wheel build +
  contents + installed-wheel smoke passed; beta release/access/hygiene gates and
  `git diff --check` clean.
- Working-tree run 2026-09-03: pytest job-9f6da366d647e49f45a9 (29 failed), unittest job-ed48491da2aa278edfc6 (21 failures, 3 errors)
- CI run URL: pending
- benchmark record: pending
- security review commit: pending

## 5. Artifact and platform matrix

- [x] Quality workflow declares Windows, macOS, and Linux jobs.
- [x] Stable release workflow now builds/smoke-tests artifacts before pushing the
  tag.
- [x] KaroX 5 release workflow requires strict product gate and packaged/runtime
  version equality.
- [x] KaroX 5 release workflow requires a wheel, installed-version check, source
  archives, and SHA-256 verification before tag creation.
- [x] `scripts/check_release_workflow.py` guards the safety ordering.
- [ ] Updated release workflow YAML is accepted by GitHub Actions.
- [ ] Wheel builds on Linux.
- [x] Wheel installs and imports outside the source tree in isolated Windows acceptance.
- [x] `karox --help` works from the installed wheel in isolated Windows smoke.
- [x] `karox-vnext --help` compatibility alias works in isolated Windows smoke.
- [ ] Separate `karox-remote` wheel or standalone artifact builds and installs
  without the KaroX runtime or a user repository.
- [ ] Windows Python 3.10/3.12/3.13/3.14 artifact matrix passes.
- [ ] macOS Python 3.10/3.12/3.13/3.14 artifact matrix passes.
- [ ] Linux Python 3.10/3.12/3.13/3.14 artifact matrix passes.
- [ ] PowerShell 5.1 launchers parse.
- [ ] POSIX shell launchers pass syntax checks.

Evidence:

- GitHub Actions run: pending

## 6. Installation, update, migration, and removal

For Windows, macOS, and Linux separately:

- [ ] Clean installation without an existing KaroX runtime.
- [ ] Missing Git produces an actionable diagnostic.
- [ ] Missing Python produces an actionable diagnostic.
- [ ] Reinstall of the same build is safe.
- [ ] Upgrade from latest stable 4.x succeeds.
- [ ] `karox migrate --json` performs no write.
- [ ] `karox migrate --apply --json` writes only validated destination metadata.
- [ ] Unsupported legacy state is reported, not silently dropped.
- [ ] Secrets remain in or are re-entered into the OS keyring.
- [ ] Interrupted staged update rolls back.
- [ ] Failed validation never activates a staged runtime.
- [ ] Active processes do not leave a half-updated installation.
- [ ] Uninstall never removes a user repository.
- [ ] Session and credential removal are separate explicit decisions where
  supported.
- [ ] Previous stable launcher remains usable during the rollback window.

Evidence records:

- Windows: pending
- macOS: pending
- Linux: pending

## 7. ChatGPT Web live conformance

- [ ] Real workspace, account tier, and role recorded.
- [ ] Current Apps/developer-mode creation path recorded.
- [ ] Discovery succeeds.
- [ ] Dynamic client registration succeeds.
- [ ] Authorization Code with PKCE succeeds.
- [ ] Approval page is understandable in English and Russian.
- [ ] MCP initialize succeeds.
- [ ] Only explicitly selected tools are listed.
- [ ] Read succeeds.
- [ ] Mutation succeeds.
- [ ] Same idempotency identity does not duplicate mutation.
- [ ] Approved check succeeds.
- [ ] Git status and diff succeed.
- [ ] Refresh rotation succeeds.
- [ ] Refresh replay revokes the token family.
- [ ] Restart behavior is recorded.
- [ ] Quick Tunnel URL change produces an actionable warning.
- [ ] Sanitized evidence is attached.

Record: `docs/conformance/chatgpt-web.md`

## 8. Claude Web live conformance

- [ ] Real account tier and organization role recorded.
- [ ] Current Settings → Connectors → Add custom connector path recorded.
- [ ] OAuth approval succeeds.
- [ ] Only explicitly selected tools are visible.
- [ ] Read succeeds.
- [ ] Mutation succeeds.
- [ ] Idempotency is verified.
- [ ] Approved check succeeds.
- [ ] Git status and diff succeed.
- [ ] Credential refresh/rotation behavior is recorded.
- [ ] Restart behavior is recorded.
- [ ] Sanitized evidence is attached.

Record: `docs/conformance/claude-web.md`

## 9. Provider and Ellipsis live conformance

For OpenAI Responses, Anthropic Messages, Gemini, and one named generic
OpenAI-compatible endpoint:

- [ ] Exact official endpoint and model ID recorded.
- [ ] Minimal real request succeeds.
- [ ] Tool/function call is received.
- [ ] Core executes the selected tool.
- [ ] Tool result is returned to the provider.
- [ ] Final model response is received.
- [ ] Usage is recorded where available.
- [ ] Credential is absent from config, output, logs, session, and evidence.
- [ ] Timeout/rate-limit behavior and limitations are documented.

Ellipsis Opus 5 local-workspace acceptance:

- [x] Repository-free create payload is contract tested.
- [x] Local bridge E2E proves read, write, check, diff, report, and revocation.
- [x] Live test refuses to run without token, explicit flag, exact confirmation,
  and a budget at or below `0.10 USD`.
- [ ] Account-specific interactive REST contract is confirmed.
- [ ] Version-pinned `karox-remote` artifact is available in the sandbox.
- [ ] Temporary unpublished local repository live test passes.
- [ ] Ellipsis receives no Git origin or repository metadata.
- [ ] Credential is revoked and local managed processes are closed on stop.

Records:

- `docs/conformance/openai-responses.md`
- `docs/conformance/anthropic-messages.md`
- `docs/conformance/gemini.md`
- `docs/conformance/openai-compatible.md`
- `docs/ELLIPSIS_LOCAL_AGENT.md`

## 10. External beta

- [x] Beta plan and structured issue template exist.
- [x] Sanitized aggregate summary exists and says beta has not started.
- [ ] Ten candidates recruited.
- [ ] Five unaided installation attempts completed.
- [ ] Three testers complete the primary scenario.
- [ ] Two complete it without maintainer intervention.
- [ ] Two voluntarily perform a second task.
- [ ] Last five attempts meet installation-blocker threshold.
- [ ] Last five attempts meet permission-confusion threshold.
- [ ] Every P0 beta issue is closed with regression evidence.
- [ ] Aggregate summary status is `passed` with required numeric thresholds.

Plan: `docs/BETA_TEST_PLAN.md`  
Summary: `docs/conformance/beta-summary.md`

## 11. Documentation and packaging

- [ ] Demo matches the release candidate.
- [ ] Stable installation commands point to the stable channel.
- [ ] Quick start succeeds from a clean machine without undocumented steps.
- [ ] Security and troubleshooting receive final review.
- [ ] Migration rehearsal matches `docs/MIGRATION_V4_TO_V5.md`.
- [ ] `CHANGELOG.md` contains the final entry.
- [ ] `RELEASE_NOTES_v5.0.0.md` exists and lists breaking changes.
- [ ] Known limitations are explicit.
- [ ] ZIP, TAR.GZ, wheel, and checksums are verified before tag push.
- [ ] GitHub release assets are verified after upload.

## 12. Final release decision

Before changing either version source to final `5.0.0`:

```bash
python scripts/check_v5_release.py --strict
```

The command must pass. Then:

- [ ] Runtime version and `VERSION` are both exactly `5.0.0`.
- [ ] Legacy server version mirror is updated by the release process.
- [ ] Final release notes exist.
- [ ] Quality workflow passes on the exact tree to be tagged.
- [ ] Artifact build and smoke test pass before tag creation.
- [ ] No untested source file is added after the quality run.
- [ ] Annotated tag points at the artifact-tested tree.
- [ ] Release assets and checksums are published.
- [ ] `RELEASE.json` records the verified published assets.
- [ ] Stable update channel resolves to the new release.
- [ ] Rollback instructions remain available.

Release decision remains **NOT READY** until every unchecked P0 item above has
reproducible evidence.
