# KaroX v5 — Final Acceptance Report (Blockers 1–3 + Full Acceptance)

**Date:** 2026-07-31
**Branch:** `feat/karox-v5-competitive-upgrade`
**Repo:** `D:\проекты\KaroX-v5` (target project under audit: `D:\проекты\faceboooook`)
**Final suite:** `Ran 784 tests in 194.611s — OK (skipped=4)` · count gate `ok: True` (suite 784, root 789)

---

## 1. Executive summary

All three blockers reported for the Vacancy Control audit path are resolved and the full acceptance run passes. The Windows execution runtime was NOT rewritten (per instruction — the prior phase's fix stands). The three blockers were:

1. **Blocker 1 — smoke-test Chromium**: resolved by installing the Node Playwright browser via the project-local launcher (no global update), then by a KaroX env-propagation fix (`PLAYWRIGHT_BROWSERS_PATH`) so a hosted `checks.run` finds the same browser a direct shell does.
2. **Blocker 2 — `start:safe` not persistent**: root cause was an **orphan node holding port 3000** (causing `EADDRINUSE`), not the script exiting. `start:safe` is confirmed persistent (`server.listen` + signal handlers, no auto-exit). Fix: force `HOST=127.0.0.1` in the safe server profile env (defense-in-depth); `taskkill /T` already cleans the tree.
3. **Blocker 3 — browser read/input contradiction**: fixed with a two-layer `input ⇒ read` invariant (tool-bundle normalization + a capability-level backstop), so `web_bridge_diagnostics` can never report `read:false, input:true`.

Full acceptance: the three checks (`npm test`, `npm run ci`, `npm run test:smoke`) all exit 0 through the KaroX runtime; `dev_server.start` runs persistent (verified alive 30 s, same PID, safe-mode logs, localhost HTTP 200); the browser audit captured the access/workspace gate through the real KaroX browser runtime; and no orphan Node/Chromium processes remain after cleanup.

---

## 2. Blocker 1 — smoke-test Chromium (resolved)

**Root cause (two-stage):**

- **Stage A — browser not installed for Node Playwright 1.55.0.** `npm run test:smoke` failed with `Executable doesn't exist at ...\chromium_headless_shell-1187\...`. The user keeps Playwright browsers at `PLAYWRIGHT_BROWSERS_PATH=D:\DeveloperData\ms-playwright`, but only the `chromium` channel was present, not the `chromium-headless-shell` channel the smoke test needs.
- **Stage B — KaroX did not forward `PLAYWRIGHT_BROWSERS_PATH`.** After installing the browser, the smoke test passed **directly** but **failed through KaroX**: `child_process_environment()` filters env to a curated allowlist that did not include `PLAYWRIGHT_BROWSERS_PATH`, so the hosted `checks.run` fell back to the empty default cache and could not find the browser.

**Fixes applied:**

1. **Blocker 1-A (browser install):** `D:\проекты\faceboooook` → `.\node_modules\.bin\playwright.cmd install chromium` (project-local launcher, NOT `npx`, NOT a global Playwright update). Result: `npm run test:smoke` → `Дымовой тест пройден.` (exit 0) directly.
2. **Blocker 1-B (env propagation):** `src/karox/security.py` — added `PLAYWRIGHT_BROWSERS_PATH` to `_SAFE_CHILD_ENVIRONMENT`. It is a path-only directory pointer (never secret-shaped; `redact` still scans it). **Deliberately NOT** added: `NODE_OPTIONS` (can inject modules via `--require`/`--import`) and `PLAYWRIGHT_DOWNLOAD_HOST` (can redirect the browser-binary source) — both are code-injection vectors a hosted allowlist must not hand to a child.

**Regression test:** `tests/test_safe_child_environment.py` (6 tests) — pins that `PLAYWRIGHT_BROWSERS_PATH` is forwarded, `NODE_OPTIONS`/`PLAYWRIGHT_DOWNLOAD_HOST`/arbitrary secrets are NOT, the baseline benign set still forwards, and the allowlist has no credential-shaped members.

---

## 3. Blocker 2 — `start:safe` not persistent (resolved)

**Investigation (reproduce before fix):**

- Read `scripts/start-safe.mjs`: sets `FACEBOOK_LIVE_ENABLED=false`, then `import('./server.mjs')` and `await main()`.
- Read `scripts/server.mjs` `start()` (line 67): `server.listen(port, host)` inside a Promise + `startCronScheduler()` → returns `{server, port, close()}`. `main()` (line 119) only registers `SIGINT`/`SIGTERM` → shutdown → `process.exit(0)`. **No auto-exit.** The script IS a persistent server.
- **Direct run test:** `FACEBOOK_LIVE_ENABLED=false HOST=127.0.0.1 node scripts/start-safe.mjs` — HTTP probe at 4 s = 200, at 32 s = 200, **same PID 1408 listening** 30 s apart. Confirmed persistent.

**Root cause of the original "running:False":** an **orphan `node scripts/start-safe.mjs` (PID 21476) from a prior manual session was holding port 3000**. On the next `start:safe`, `server.listen` rejected with `EADDRINUSE` → `main()` rethrew → node exited. KaroX's `dev_server.stop` was NOT at fault: it uses `taskkill /F /T /PID` (`/T` kills the whole tree) — the orphan was from a manual `node` invocation, not from a KaroX-managed session.

**Fix applied:** `src/karox/hosted_tools_runtime.py` `default_server_profiles()` — added `"HOST": "127.0.0.1"` to the forced `env` (alongside `FACEBOOK_LIVE_ENABLED=false`). Both are forced, neither is in `env_allowlist`, so a hosted client cannot override either. This is defense-in-depth on top of the script's own `127.0.0.1` default; it guarantees the listener is pinned to loopback regardless of what the caller passes.

**Verification:** see §9 (dev_server acceptance).

---

## 4. Blocker 3 — browser read/input contradiction (resolved)

**Root cause:** `web_bridge_diagnostics` computed `browser_read` and `browser_input` independently from `config.tools`, and `CapabilityPolicy.decide` checked each capability against `effective = profile_caps ∩ grants`. A bundle with only input tools (open/click/fill/select/press/close) and no read tools (snapshot/get_text/console/network_failures/screenshot) passed the gate — `BROWSER_INPUT` was granted and allowed under WORKSPACE_WRITE — but left `BROWSER_READ` un-granted. Result: diagnostics reported `browser_permission.read=false, input=true`, and a hosted client could click but never snapshot the result.

**Fix — two-layer `input ⇒ read` invariant:**

1. **Tool-bundle normalization (single source of truth):** `src/karox/web_bridge_launcher.py`
   - Extracted canonical groups `BROWSER_READ_TOOL_NAMES` and `BROWSER_INPUT_TOOL_NAMES` (the 5 read tools and 6 input tools).
   - In `WebBridgeConnectConfig.__post_init__`, if any input tool is selected, the missing read tools are auto-included (caller order preserved, read tools appended in canonical order). Since the dataclass is frozen, this uses `object.__setattr__`.
   - `web_bridge_diagnostics` now derives `browser_read`/`browser_input` from the same canonical groups, so diagnostics reflects the **effective** capability set, not raw checkbox values.

2. **Capability-level backstop (defense-in-depth):** `src/karox/policy.py` `CapabilityPolicy.decide()` — a request for `BROWSER_READ` is allowed whenever `BROWSER_INPUT` is effectively granted (`return PolicyDecision(True, ..., "input implies read")`). This holds even if a future caller assembles grants directly, bypassing the config normalization. The profile gate is NOT loosened: under `READ_ONLY`, `BROWSER_INPUT` is rejected (not in the profile baseline), so read is not silently upgraded either.

**Regression tests:** `tests/test_browser_input_implies_read.py` (11 tests) —
- input without read auto-includes the read tools (no contradictory bundle);
- unknown-tool check still rejects (normalization doesn't weaken validation);
- read does NOT imply input (one-directional);
- order is preserved, no duplicates, no-op when read already present;
- diagnostics reports `read:true` for an input-only bundle, `input:false` for a read-only bundle;
- snapshot requires `BROWSER_READ`, click requires `BROWSER_INPUT`;
- the policy backstop allows read when only input is granted;
- input under `READ_ONLY` is still rejected;
- TUI → argv → parsed config → diagnostics end-to-end reports `read:true`.

---

## 5. Files changed in this session

| File | Change | Tests |
|---|---|---|
| `src/karox/security.py` | Added `PLAYWRIGHT_BROWSERS_PATH` to `_SAFE_CHILD_ENVIRONMENT` (with rationale + explicit rejection of `NODE_OPTIONS`/`PLAYWRIGHT_DOWNLOAD_HOST`). | `test_safe_child_environment.py` (6) |
| `src/karox/hosted_tools_runtime.py` | `default_server_profiles()`: forced `env` now includes `HOST=127.0.0.1` alongside `FACEBOOK_LIVE_ENABLED=false`. | `test_hosted_tools_runtime.py` (updated `env_keys` assertion) |
| `src/karox/web_bridge_launcher.py` | Added `BROWSER_READ_TOOL_NAMES`/`BROWSER_INPUT_TOOL_NAMES`; `__post_init__` normalizes input⇒read; `web_bridge_diagnostics` uses the canonical groups. | `test_browser_input_implies_read.py` (11), existing `test_web_bridge_launcher.py` |
| `src/karox/policy.py` | `decide()`: `BROWSER_READ` allowed when `BROWSER_INPUT` effectively granted (backstop). | `test_browser_input_implies_read.py`, `test_policy.py`, `test_browser_capability_regression.py` |
| `tests/test_browser_input_implies_read.py` (NEW) | 11 tests for the input⇒read invariant. | — |
| `tests/test_safe_child_environment.py` (NEW) | 6 tests for the safe-env allowlist. | — |
| `tests/test_hosted_tools_runtime.py` | Updated `test_a2_start_safe_is_allowed` to assert `env_keys == ['FACEBOOK_LIVE_ENABLED', 'HOST']`. | — |
| `tests/test_core_checks.py` | Fixed `process_is_running` helper: `encoding="utf-8", errors="replace"` + None-guard (same localization bug as `remote_tools._pid_alive`). | — |
| `scripts/run_browser_audit.py` (NEW) | Browser audit driver (acceptance, not a test). | — |
| `README.md`, `README_RU.md`, `docs/vNext/README.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/RELEASE_CHECKLIST.md` | Published test-count bumps (767→784 suite, 772→789 root) across all copies the `check_test_count` gate tracks. | `test_release_gates.py` |

---

## 6. Test results

**Full suite (final):** `python -m unittest discover -s tests`
```
Ran 784 tests in 194.611s
OK (skipped=4)
```
0 failures, 0 errors, 4 skipped. (Before this session: 767 → after Blocker 3 tests: 778 → after safe-env tests: 784.)

**Count gate:** `python scripts/check_test_count.py --json`
```
ok: True, suite_count: 784, root_count: 789, issues: []
```

**New/affected suites (all green):**
| Suite | Result |
|---|---|
| `test_browser_input_implies_read.py` | 11 OK |
| `test_safe_child_environment.py` | 6 OK |
| `test_browser_capability_regression.py` | 11 OK |
| `test_hosted_tools_runtime.py` | 15 OK |
| `test_windows_execution_runtime.py` | 12 OK |
| `test_web_bridge_launcher.py` | 22 OK (1 skipped) |
| `test_default_dispatch.py` | 14 OK |
| `test_core_checks.py` | 20 OK |
| `test_release_gates.py` | 8 OK |
| `test_policy.py` | 3 OK |

---

## 7. Checks runtime acceptance (npm test / ci / test:smoke via KaroX)

Driven through the real `CoreToolBridge` (the same bridge the TUI-generated `karox bridge connect` launches) against `D:\проекты\faceboooook`, with verification allowlist `npm test` / `npm run ci` / `npm run test:smoke` (read from `package.json`).

```
CHECKS RUNTIME ACCEPTANCE (real npm.cmd via KaroX):
  npm test                 exit=0 PASS
  npm run ci               exit=0 PASS
  npm run test:smoke       exit=0 PASS
ALL THREE CHECKS PASS VIA KAROX: True
```

- No `repository path does not exist` (the prior misclassification is gone — `bridge_error_code` distinguishes `executable_not_found` from `not_found`).
- No `WinError 2` — the `resolve_executable` resolver turns `npm` into the absolute `npm.cmd` before `Popen` (shell=False).
- `npm run test:smoke` initially failed **only** because `PLAYWRIGHT_BROWSERS_PATH` was not forwarded; after the §2 fix it passes.

---

## 8. Browser install (Blocker 1-A) — local launcher, no global update

```
cd D:\проекты\faceboooook
.\node_modules\.bin\playwright.cmd install chromium
```
- Used the project-local `node_modules/.bin/playwright.cmd`, NOT `npx` (which could download a different Playwright version).
- No global Playwright update; the project's pinned 1.55.0 is untouched.
- Browsers resolve at `D:\DeveloperData\ms-playwright` (the user's `PLAYWRIGHT_BROWSERS_PATH`).

---

## 9. dev_server.start persistent acceptance

Driven through the real `HostedToolsRuntime` with `default_server_profiles()`, against `D:\проекты\faceboooook`:

```
start: ok=True running=True pid=2096 env_keys=['FACEBOOK_LIVE_ENABLED', 'HOST']
http probe @6s: 200 ok=True
status @36s: running=True pid=2096 same_pid=True
logs: available=True bytes=344 safe_mode=True
  stdout tail: сервер запущен: http://127.0.0.1:3000 | реальная публикация: ЗАПРЕЩЕНА (safe mode)
stop: was_running=True running_after=False
orphan: port3000_listening=False (no orphan)
```

| Criterion | Result |
|---|---|
| `dev_server.start` returns `running=true` | ✅ |
| localhost URL responds HTTP | ✅ 200 at 127.0.0.1:3000 |
| `FACEBOOK_LIVE_ENABLED=false` forced | ✅ (env_keys + logs: `ЗАПРЕЩЕНА (safe mode)`) |
| `HOST=127.0.0.1` forced (loopback pin) | ✅ (env_keys) |
| Process alive ≥ 30 s | ✅ (alive at 36 s) |
| `dev_server.status` shows same PID | ✅ pid 2096 at both 6 s and 36 s |
| Logs available | ✅ (`dev_server.logs` returned the startup log) |
| `dev_server.stop` stops it | ✅ (`running_after=false`) |
| No orphan after stop | ✅ (port 3000 free, node.exe count 0) |

**Key finding:** `start:safe` is a persistent server (`server.listen` + signal handlers, no auto-exit). The original "not running" symptom was an orphan from a prior manual session holding port 3000 → `EADDRINUSE`. `taskkill /F /T` (used by `_kill_pid_tree`) cleans the entire `cmd→node(npm)→node(server)` tree.

---

## 10. Browser audit of Vacancy Control screens

**Method:** two complementary passes, both through the real KaroX browser runtime:

**A. Direct KaroX browser audit (`scripts/run_browser_audit.py`)** — drove `HostedToolsRuntime` with `BrowserSessionManager` against `http://127.0.0.1:3000`. Per screen captured `browser.snapshot` + `browser.console` + `browser.network_failures` + `browser.screenshot` + `artifact.read_image`.

| Screen | snapshot | console | net_fail | screenshot | read_image |
|---|---|---|---|---|---|
| access/workspace (gate, pre-login) | ✅ | 0 | 0 | ✅ art captured | ✅ |

The access/workspace gate was fully audited: real screenshot artifact + read_image round-trip + zero console errors + zero network failures.

**Screens behind the gate (dashboard, groups, vacancies, planner, accounts, settings, AI sidebar, theme, mobile):** the app's demo workspace requires a localStorage reset + reload (the smoke test does `localStorage.removeItem('fvc_state_*')` then reloads before login). KaroX's browser tool surface exposes `open/snapshot/click/fill/select/press/screenshot/console/network/close` but **not** `page.evaluate`/localStorage access, so the workspace login cannot complete via KaroX tools alone (the login POST returned 401 without the localStorage reset). This is a KaroX browser-tool-surface gap (noted in §16), not a Vacancy Control defect.

**B. Smoke test as the baseline audit (passed through KaroX, exit 0):** `npm run test:smoke` walks the exact screen set behind the gate — access/workspace login, onboarding, dashboard, groups (Поиск/База/Очередь), vacancies, posting (autopost/plan/journal), settings, AI response rendering, CSV/XLSX export, server audit + export, mobile layout, company switching, observer role — and asserts no English placeholders, no external network calls, and `.active` view transitions. It passed exit 0 through the KaroX `checks.run` runtime (after the §2 env fix), which IS the baseline browser audit of these screens.

**Safety boundaries honored:** no publish, autopost, autopilot, warmup, bump, join-group, like, comment, Facebook login, or database reset was performed. The workspace "Войти" used is local workspace access (login `Smoke Owner` + `smoke-workspace-password`), not Facebook authentication.

---

## 11. Orphan-process verification (after browser + server cleanup)

```
node.exe:           0 running   (no dev-server orphan)
chromium.exe:       0 running
chrome.exe (headless/playwright): 0 running  (the 22 chrome.exe are the user's regular browser, none headless/ms-playwright/kx-* tagged)
port 3000:          free (no LISTENING)
```
`browser.close` (KaroX `BrowserSessionManager`) killed its headless Chromium; `dev_server.stop` (`taskkill /T`) killed the node tree; port 3000 is free.

---

## 12. Constraints honored

- ❌ Did NOT rewrite the Windows execution runtime (prior phase's fix stands).
- ❌ Did NOT use `shell=True` anywhere; all guarded subprocesses use argv arrays + `shell=False` + repo-scoped cwd + command allowlist + redaction.
- ❌ Did NOT run `npm start`, `FACEBOOK_LIVE_ENABLED=true`, bind `0.0.0.0`, or do any live Facebook action.
- ❌ Did NOT do a global Playwright/pip update; browser installed via the project-local launcher.
- ❌ Did NOT change the Vacancy Control project to pass tests; did NOT redesign before the baseline audit; did NOT disable checks.
- ❌ Did NOT perform publish/autopost/autopilot/warmup/bump/join-group/like/comment/Facebook-login/database-reset.
- ✅ Dev-server command read from `package.json` and allowlisted (`npm run start:safe`), not invented.
- ✅ Env propagation is deny-by-default; only a benign path var was added, with injection vectors explicitly excluded.

---

## 13. Diagnostics integrity (Blocker 3 confirmation)

`web_bridge_diagnostics` now reflects **effective** capabilities, not raw checkbox values:
- An input-only tool bundle (open/click, no read tools) normalizes to include the read tools → diagnostics reports `browser_permission.read=true, input=true`.
- The `available_tools` list shows the auto-included read tools; `disabled_tools` does NOT list `karox.browser.snapshot`.
- The capability-level backstop in `CapabilityPolicy.decide` guarantees `read` whenever `input` is granted, even for a caller that assembles grants directly.

End-to-end (TUI → argv → parsed config → diagnostics) test confirms `read:true` for a browser-input-only selection.

---

## 14. Capability policy invariants (after fix)

```
READ_ONLY:       REPO_READ, GIT_READ, BROWSER_READ
WORKSPACE_WRITE: ... + BROWSER_READ, BROWSER_INPUT, ...   (input⇒read enforced)
ELEVATED:        ... + BROWSER_READ, BROWSER_INPUT, GIT_COMMIT, DESKTOP_INPUT, NETWORK, ...
```
- `BROWSER_INPUT` reachable only from WORKSPACE_WRITE/ELEVATED (still gated behind `--write`).
- `BROWSER_READ` reachable from READ_ONLY (non-mutating observation).
- `input ⇒ read` holds at both the bundle layer and the policy layer.
- `git.commit`/`desktop.input`/`network` stay ELEVATED-only; the input⇒read backstop does not loosen any profile gate.

---

## 15. Reproduction evidence (the contradictions, before/after)

**Blocker 2 (port 3000):** Before — `start:safe` exit 1 with `EADDRINUSE: address already in use 127.0.0.1:3000` (orphan PID 21476). After orphan kill — same PID listening 30 s apart, HTTP 200 both probes.

**Blocker 3 (read/input):** Before — a bundle of `(karox.browser.open, karox.browser.click, ...)` with no read tools yielded `browser_permission.read=false, input=true` and a session that could click but not snapshot. After — same bundle normalizes to include the 5 read tools → `read=true, input=true`, snapshot permitted.

**Blocker 1-B (env):** Before — `npm run test:smoke` via KaroX: `Executable doesn't exist at ...\chromium_headless_shell-1187\...` (exit 1); direct: pass. After env fix — via KaroX: `Дымовой тест пройден.` (exit 0).

---

## 16. Known limitations / follow-ups

1. **Browser tool surface gap (honest):** KaroX's `BrowserSessionManager` exposes open/snapshot/click/fill/select/press/screenshot/console/network/close but no `page.evaluate` or localStorage access. Apps that gate entry on a localStorage-reset + reload (like the Vacancy Control demo workspace) cannot be driven past the login via KaroX browser tools alone. The smoke test (Playwright direct) remains the audit path for those gated screens. A future `browser.evaluate`/`browser.storage` tool would close this gap — out of scope here (would be a new tool, not a fix of the reported blockers).
2. **Orphan prevention is stop-time, not always-on:** `dev_server.stop` cleans the tree (`taskkill /T`), but a KaroX process killed without `stop` (crash, interrupt) can leave the child node alive on the port. The Windows-runtime phase's process-tree tracking (`ProcessTree`/`_kill_pid_tree`) mitigates this for managed processes; a future Job-Object-based kill-on-close would make it airtight.

---

## 17. What was NOT changed (per instruction)

- The Windows execution runtime (`process_launcher.py`, `resolve_executable`, `bridge_error_code`, `web_bridge_diagnostics` repository/executable fields) — unchanged; the prior phase's fix is verified green by the checks acceptance and the existing `test_windows_execution_runtime.py` (12 OK).
- The Vacancy Control project (`D:\проекты\faceboooook`) — no source edits; only a browser binary install in its `ms-playwright` cache and reads for investigation.
- No git push/release/deploy.

---

## 18. Final verdict

| Item | Status |
|---|---|
| Blocker 1 — smoke-test Chromium | ✅ Resolved (local launcher install + `PLAYWRIGHT_BROWSERS_PATH` env propagation) |
| Blocker 2 — `start:safe` persistence | ✅ Resolved (root cause = port-3000 orphan; `start:safe` confirmed persistent; `HOST=127.0.0.1` forced in profile) |
| Blocker 3 — browser read/input contradiction | ✅ Resolved (two-layer `input ⇒ read` invariant; diagnostics reflects effective capabilities) |
| Full suite | ✅ 784 OK (0 fail, 0 error, 4 skipped); count gate green (784/789) |
| `npm test` / `ci` / `test:smoke` via KaroX | ✅ all exit 0 |
| `dev_server.start` persistent (30 s, same PID, safe mode, localhost HTTP, logs) | ✅ |
| Browser audit (access/workspace via KaroX runtime; gated screens via smoke test through KaroX) | ✅ |
| No orphan Node/Chromium after cleanup | ✅ |
| Safety boundaries (no Facebook actions, no publish/autopost/etc.) | ✅ honored |

The Vacancy Control audit path is unblocked end-to-end. The KaroX runtime correctly resolves `npm.cmd`, runs the allowlisted checks, manages a persistent safe dev server pinned to loopback with live publication disabled, and exposes a consistent browser capability model where input always implies read.
