# Commit plan — working tree as of 2026-08-20 (release-live-pass3)

The branch `feat/karox-v5-competitive-upgrade` carries ~334 dirty files;
the last commit is dated 2026-08-02. Nothing here is committed automatically:
per the working-tree rule in V5_MASTER_EXECUTION_STATE section 1, commits
require Egor's explicit go-ahead. This is the prepared, reviewable plan.

## Suggested logical commits (in order)

1. `feat(access): unified Bypass/access-mode contract`
   - src/karox/access_mode.py, registry.py (ProviderRecord.bypass),
     tui_connections.py (advanced layer), related call-site edits
   - tests: test_access_mode.py, test_bypass_matrix.py, test_bypass_wiring.py,
     test_tui_advanced_connections.py

2. `feat(projects): multi-project registry, leases, workspace manager`
   - project_registry.py, project_context.py, repository_lease.py,
     workspace_change_guard.py, tui_workspace.py, web_bridge_profiles.py edits
   - tests: test_project_registry.py, test_project_context.py,
     test_repository_lease.py, test_workspace_*.py, test_multi_project_bridge.py

3. `feat(economy): cost intelligence CI-0..CI-5 and telemetry`
   - cost_intelligence.py, tool_telemetry.py, usage_analytics.py, context_ab.py
   - tests: test_cost_intelligence*/test_tool_telemetry/test_context_ab

4. `feat(intelligence): repo context map, task state, checkpoints, plans`
   - repo_context.py, task_state.py, checkpoints.py, plan_executor.py,
     plan_act.py, result_envelope.py, verification.py
   - tests: matching test files

5. `feat(runtime): autonomy runtime, smart stop, detached processes, routes`
   - autonomy_runtime.py, smart_stop.py, detached_process.py, port_ownership.py,
     route_health.py, tailscale_routes.py, runtime_restart.py,
     saved_bridge_autostart.py, saved_bridge_supervisor.py, check_jobs.py
   - tests: matching test files

6. `feat(browser): bootstrap, credentials, managed browser, extension`
   - browser_bootstrap.py, browser_credential*.py, managed_browser.py,
     browser_extension/*.js additions
   - tests: matching test files

7. `feat(connections): notion/mcp oauth, session view, dashboards`
   - notion_mcp.py, mcp_oauth.py, connection_status.py, session_view.py,
     tui_dashboard.py, clipboard.py, transcript*.py, research_subagent.py

8. `chore(scripts): portable bundle, coverage gate, acceptance runners`
   - scripts/* additions (drop scripts/b6_diag*.py leftovers or commit under
     a diagnostics/ note)

9. `fix(isolation): enforce production/test isolation (this session)`
   - tests/_path_setup.py, src/karox/paths.py, src/karox/credentials.py,
     tests/test_production_isolation_guard.py, test_web_bridge_launcher.py,
     test_credentials.py
   - .gitattributes (new), .gitignore junk rules

10. `docs: master state, evidence, checklists`
    - docs/* modified + new evidence files, benchmarks/ (decide whether
      benchmark JSON snapshots belong in git or in artifacts)

## Do NOT commit
- miraism/ (foreign project), NUL, OPUS5_*/OUTREACH_*/TARGET_MODEL_*.md,
  PROJECT_CONTEXT.md, .commandcode/ — now gitignored; consider moving the
  outreach documents out of the repository directory entirely.
- .gitlab/ — decide: is GitLab CI real for this repo? If not, remove.
