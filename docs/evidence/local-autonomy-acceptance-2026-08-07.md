# KaroX 5 local autonomy acceptance — 2026-08-07

Status: **PASSED for the local/isolated Windows acceptance slice recorded here.**

This record is intentionally narrower than the product release decision. Remote
GitHub Actions for this exact dirty working tree, named-product live conformance,
external beta, installation/migration rehearsals, the real paired ChatGPT Web
benchmark, and any migration of the live `clickup-opus` profile remain separate
gates.

## Scope

- Repository: `D:\проекты\KaroX-v5`
- Branch: `feat/karox-v5-competitive-upgrade`
- HEAD: `1b01350` (`feat(bridge): enforce Smart Stop for hosted clients by default`)
- Working tree: dirty; this acceptance covers the exact local working tree at the
  time of the runs, not a pushed commit.
- Platform: Windows, Python 3.13.
- Live saved profile `clickup-opus`: not migrated, restarted, or reconfigured by
  the acceptance automation.

## Managed full suite

The current-tree full pytest suite was launched by the explicit detached harness
`tests/managed_full_suite_launcher.py` and executed through the managed
`karox.checks.start/status/logs` path of a separate local bridge.

- command: `python -m pytest`
- result: **2197 passed, 5 skipped, 1544 subtests passed**
- pytest duration: `599.49 s`
- managed job duration: `605.797 s`
- exit code: `0`
- first failure: none
- isolated bridge PID: `11700 -> 11700`
- bridge alive after suite: yes
- runtime healthy after suite: yes
- managed artifact: `art-213183c91b196328d21f`
- full output: `scratch/full_suite_managed_acceptance_current_output.txt`
- output SHA-256: `71abcdd010d3564692494c296fb141223764ee5ed368651659c7b5de48c5ac1a`
- machine-readable record: `scratch/full_suite_managed_acceptance_current.json`

The managed request was detached from the initiating MCP request and used a
Windows new process group, breakaway process, no-window creation, and an owned
Job Object. Runtime probes remained green throughout the run.

## Canonical coverage gate

Coverage used the same canonical discovery command as the quality workflow and
ran through a separate managed bridge job:

`python -m coverage run -m unittest discover -s tests -p "test_*.py"`

- canonical suite result: **OK, 5 skipped**
- configured branch-aware gate: `70%`
- measured coverage: **72.41954542295127%**
- statements: `24565 / 32541` covered
- branches: `6692 / 10620` covered
- managed job duration: `695.744 s`
- exit code: `0`
- isolated bridge PID: `4740 -> 4740`
- bridge alive after coverage: yes
- runtime healthy after coverage: yes
- managed artifact: `art-25199ecbe95816298329`
- report: `scratch/coverage_current.json`
- full output: `scratch/coverage_managed_acceptance_latest_output.txt`
- output SHA-256: `f67e7f94c7f2d79deea2560cd1780efdff6454c9f4e295692694459e9603187f`
- machine-readable record: `scratch/coverage_managed_acceptance_latest.json`

The first coverage attempt exposed three test-harness defects rather than a
runtime regression: two saved-profile discovery tests depended on a real local
`clickup-opus` profile, and the four-second lifecycle smoke had insufficient
margin under instrumented load. The discovery tests now use temporary saved
profile fixtures and the lifecycle smoke uses a twelve-second job with a
three-second threshold probe. The focused regression run then passed:
`24 passed, 1 skipped`.

## Other local gates

- Release gates: **11 passed** via `python -m pytest tests/test_release_gates.py` after support-bundle hardening.
- Ruff: **passed** (`python -m ruff check src tests scripts`).
- Mypy: **passed**, 96 source files via `python -m mypy src/karox`; no `src/karox` source changed after that typing run in this acceptance continuation.
- Windows platform-sensitive focused slice: **214 passed, 6 subtests passed** via `python -m pytest tests/test_check_jobs.py tests/test_process_identity.py tests/test_managed_browser.py tests/test_extension_browser.py`.
- Support-bundle confidentiality: adversarial synthetic-secret gate **passed**;
  the public `karox support` route uses the hardened exporter.
- Wheel build: `karox_runtime-5.0.0.dev0-py3-none-any.whl` **built successfully** via `python -m build --wheel`.
- Real wheel/source contents gate: **passed** through explicit non-discovery
  `tests/actual_wheel_contents_acceptance.py`, which invokes
  `scripts/check_wheel_contents.py` against the current `dist` wheel.
- Installed-wheel autonomy acceptance: **passed** via `python -m pytest tests/installed_wheel_autonomy_acceptance.py` from a non-editable temporary venv outside the source tree, including MCP initialize/tool listing, low-level
  tools, high-level autonomy tools, managed check jobs, lease conflict/recovery,
  idempotent replay, restart recovery, and task resume/status.
- Published canonical test count remains `2199` suite / `2204` root; the explicit
  managed launchers and installed-wheel acceptance harness deliberately do not
  match `test_*.py` discovery.

## CI and packaging review

Local workflow definitions were modernized to current action majors and the
artifact matrix now covers Python `3.10`, `3.12`, `3.13`, and `3.14` on Ubuntu,
Windows, and macOS. The representative cross-platform contract matrix remains
Python `3.10` and `3.12` on all three OS families. The additional `ci.yml` now
installs dependency groups correctly and checks the real freshly built wheel
with `scripts/check_wheel_contents.py` instead of relying only on its synthetic
unit test.

This is local source review only. **GitHub Actions has not executed this exact
working tree because no push was performed.**

## Explicitly not claimed

- Remote CI for this exact tree: **NOT PROVEN WITHOUT PUSH**.
- Linux/macOS runtime result for this exact tree: **NOT PROVEN LOCALLY**.
- Real paired ChatGPT Web benchmark: **NOT RUN — USER GATE**.
- Live `clickup-opus` migration to the new high-level/managed tool set:
  **NOT PERFORMED — USER GATE**.
- Full KaroX 5 product release decision: **NOT READY** until the remaining
  release checklist, live conformance, installation/lifecycle, provider, beta,
  and external evidence gates are satisfied.
