# KaroX 5 commit plan

Prepared 2026-08-02. The branch carries a large multi-session tree that has
never been committed. This file turns it into an ordered set of logical
commits so history explains the work instead of burying it in one blob.

## Why KaroX cannot run these itself

The hosted bridge deliberately cannot commit. `bridge.diagnostics` on the
`clickup-opus` profile reports:

- `karox.git.commit` → disabled, "not selected by the connection profile";
- `access_profile` → `workspace_write`, and `GIT_COMMIT` is granted only to
  `elevated` in `karox.policy`;
- `mode_restrictions.no_git_push` → true.

So committing is a two-lock decision: the profile must select the tool *and*
the access profile must grant the capability. That is the design working, not a
bug. Run the commands below in PowerShell from the repository root.

## Before the first commit

```powershell
git status --short --branch
```

Everything below is `git add` of explicit paths. Nothing uses `git add -A`, so
nothing unexpected can enter a commit. No command here pushes.

## 1. Safety core: source-independent Smart Stop and the event stream

```powershell
git add src/karox/risk_engine.py src/karox/risk_mapping.py src/karox/event_bus.py
git add tests/test_risk_engine.py tests/test_risk_mapping.py tests/test_event_bus.py
git commit -m "feat(safety): one risk engine and one event stream for every agent source"
```

## 2. Enforce Smart Stop at the Core boundary

```powershell
git add src/karox/core.py src/karox/models.py tests/test_core_smart_stop.py
git commit -m "feat(core): gate every command through Smart Stop before the mutation lease"
```

## 3. Strong process identity and connection lifecycle

```powershell
git add src/karox/process_identity.py src/karox/connection_runtime.py
git add src/karox/connection_controller.py src/karox/launch_support.py
git add tests/test_process_identity.py tests/test_connection_runtime.py
git add tests/test_connection_controller.py tests/test_launch_support.py
git commit -m "feat(connections): prove process identity instead of trusting a stored PID"
```

## 4. Connections and provider surfaces

```powershell
git add src/karox/connections.py src/karox/connection_tests.py src/karox/clickup_setup.py
git add src/karox/provider_controller.py src/karox/tui_connections.py
git add tests/test_connections.py tests/test_connections_cli.py tests/test_connection_tests.py
git add tests/test_connection_tests_model.py tests/test_clickup_setup.py
git add tests/test_provider_controller.py tests/test_tui_connections.py
git add tests/test_tui_clickup.py tests/test_tui_clickup_connection_screen.py
git add tests/test_oauth_clickup_redirect_chain.py
git commit -m "feat(connections): one Connections centre over a shared controller"
```

## 5. Browser access, extension and Chrome control

```powershell
git add src/karox/browser_access.py src/karox/extension_browser.py src/karox/system_chrome.py
git add src/karox/browser_extension/
git add tests/test_external_browser_access.py tests/test_extension_browser.py
git add tests/manual_extension_browser_smoke.py tests/manual_external_browser_smoke.py
git add tests/manual_external_browser_example_smoke.py
git add docs/EXTERNAL_BROWSER.md
git commit -m "feat(browser): external browser access with an isolated session contract"
```

## 6. Developer runtime: hot worker, transactions, unified patch

```powershell
git add src/karox/hot_worker.py src/karox/workspace_worker.py
git add src/karox/workspace_transaction.py src/karox/unified_patch.py
git add tests/test_developer_runtime.py
git commit -m "feat(runtime): hot-reloadable worker with transactional repository writes"
```

## 7. Bridge, policy and packaging changes

```powershell
git add src/karox/bridge.py src/karox/cli.py src/karox/core_tools.py
git add src/karox/hosted_bridge.py src/karox/hosted_tools_runtime.py
git add src/karox/mcp_client.py src/karox/oauth_bridge.py src/karox/policy.py
git add src/karox/proxy_server.py src/karox/tui.py
git add src/karox/web_bridge_launcher.py src/karox/web_bridge_profiles.py
git add pyproject.toml
git add tests/test_bridge.py tests/test_bridge_tool_normalization.py
git add tests/test_extended_core_tools.py tests/test_hosted_bridge.py
git add tests/test_hyperagent_bridge.py tests/test_oauth_bridge.py
git add tests/test_tui.py tests/test_tui_selection.py
git add tests/test_web_bridge_launcher.py tests/test_web_bridge_profiles.py
git add tests/test_windows_execution_runtime.py
git commit -m "feat(bridge): durable developer profile and a stable guarded tool surface"
```

## 8. Release gates and documentation

```powershell
git add scripts/check_wheel_contents.py tests/test_wheel_contents.py
git add tests/test_release_gates.py
git add README.md README_RU.md
git add docs/CONNECTIVITY.md docs/IMPLEMENTATION_STATUS.md docs/RELEASE_CHECKLIST.md
git add docs/vNext/README.md docs/V5_MASTER_EXECUTION_STATE.md docs/V5_COMMIT_PLAN.md
git add docs/KAROX_V5_RECOVERY_PLAN_2026-08-01.md docs/research/
git commit -m "chore(release): verify the wheel against source and republish real counts"
```

## After the last commit

```powershell
git status --short
git log --oneline -8
```

`git status` should be empty. If anything remains, it is a file this plan did
not classify: read it before adding it, do not sweep it in.

**Do not push.** No step here pushes, and the bridge blocks push by design. A
push is a separate decision, made after the release checklist is satisfied.

## Known caveat: line endings

`src/karox/core.py` and `src/karox/tui_connections.py` are stored with CRLF
while new files are LF. Committing does not fix that, and a later normalization
pass will produce a large whitespace-only diff. Do that as its own commit,
together with a `.gitattributes` rule, so it never mixes with real changes.
