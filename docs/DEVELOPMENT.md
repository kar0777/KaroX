# Development and release gates

## Setup

```text
cd D:\проекты\KaroX-v5\lab\kernel-duel-20261025\integrate
python -m venv .venv
. .venv/Scripts/activate         # Windows
python -m pip install -e ".[dev]"
```

The package is `karox` and the version source is `src/karox/__init__.py`.
`pyproject.toml` reads it dynamically; do not duplicate the number.

For live development runs, the opt-in routes and the local credential file are
documented in [DEV_PROVIDERS.md](DEV_PROVIDERS.md). They are not product
defaults and are never required for the test suite.

## While editing

```text
python -m ruff check src tests
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python -m pytest tests/test_tui.py tests/test_cli.py
```

The copied kernel is not in mypy's file list yet. It is excused by scope, not by
`ignore_errors`; `follow_imports = "skip"` keeps mypy from checking files outside
the declared scope, and moving a module into the list is how that debt is paid
down. The suite currently collects 1,542 test items on Windows (15 skipped in the clean run); the two tests of
`tests/test_computer_use_live.py` are Windows-only and fail to import elsewhere.

## Full gate

```text
python -m ruff check src tests scripts
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python -m pytest
python scripts/check_tree.py
python -m build --wheel
python -m zipfile -l dist/karox_runtime-*.whl
```

Every gate is deterministic. A flaky-looking failure is still a failure until
it is reproduced and explained.

The package-size ceiling is declared once in `scripts/check_tree.py`
(`MAX_PACKAGE_LINES`) and `tests/test_tree.py` loads that module, so the script
and the test share one count and one limit. Raising it is a deliberate policy
change recorded in the audit, never a way to let a stray copy or a generated
dump pass.

Bridge tests never open a public route. For a non-live local check:

```text
karox bridge connect chatgpt-web --repository . --tunnel custom \
  --public-url https://example.test --diagnostics-only --json
```

Do not run `bridge connect --tunnel tailscale` against a live connector as part
of a test: it creates a real Funnel route and a real keyring entry.

## Offline headless quick gate (browser-free subset)

The full gate above is the release gate. On a machine with managed Chromium
installed, a broad `pytest` selection can start a real browser even when the
selection was meant to be local: on 2026-11-06 three scenarios in
`tests/test_engine_tools.py` did exactly that because their ready-check passed.
This subset is the explicit, pre-audited, browser-free smoke. It is a subset,
not a replacement for the full gate, and passing it is not `RELEASE_READY`.

Run it sequentially (no `-n`/xdist), with the project `addopts` disabled and no
pytest cache writes:

```text
python -m pytest -o addopts="" -p no:cacheprovider tests/test_tree.py tests/test_tool_shape_fixes.py tests/test_session_export.py tests/test_agent_prompt.py tests/test_agent_report_surface.py tests/test_engine_provider_accounting.py tests/test_engine_tool_env_render.py tests/test_run_reuse_identity.py tests/test_skill_scope.py tests/test_unified_patch_leniency.py tests/test_package_data.py tests/test_headless_selection.py
```

The command names every file; do not replace it with `-k`, a glob or a whole
directory, and do not add `tests/test_engine_tools.py`, `tests/test_browser_*.py`,
`tests/test_computer_use*.py`, `tests/test_cli.py` or any module that opens a
page, a desktop window, a listening socket or a provider. The allowlist is
pinned by `tests/test_headless_selection.py`, which fails if the docs command
and the audited list drift apart, and which records the three Mission 07
browser incidents as a negative control:

- `test_cli_engine_run_advertises_and_executes_browser_command`;
- `test_browser_command_entry_point_opens_a_real_page`;
- `test_cli_engine_run_logs_in_and_downloads_a_report`.

A platform-only skip (POSIX symlink/permission checks) is acceptable; a real
failure is not. Use the full gate before release.

## Live smoke

Use an isolated configuration so the developer's real providers are untouched:

```text
export KAROX_CONFIG_DIR=/tmp/karox-smoke/config
export KAROX_RUNTIME_DIR=/tmp/karox-smoke/runtime
export KAROX_PROVIDER_GATEWAY_API_KEY=...

karox provider add gateway --adapter openai_compatible_chat \
    --base-url http://127.0.0.1:4000/v1 \
    --credential-ref env:KAROX_PROVIDER_GATEWAY_API_KEY
karox model add gateway <model> --tools true --streaming true \
    --context-window 1048576 --max-output-tokens 32768
karox model select gateway <model>
```

Then, in a throwaway Git repository with a test that checks the target file:

```text
karox agent run --repository /tmp/repo \
    --task "Create hello.txt whose only content is hi and a newline" \
    --verification-command '["python","-m","pytest","-q"]' \
    --stream-json
```

Success means the final line is a result document with `"verified": true`, the
check ran, and the file exists with exactly the requested bytes. The terminal
client is checked by running the same task through `karox tui` and reading the
result card.

A bridge smoke that does not need a public route can start `bridge serve` on a
loopback port, complete OAuth discovery/DCR/PKCE against that port, call
`tools/list` and one read-only `tools/call`, then stop the child. The release
tests cover the same wire in-process; the manual HTTP pass is recorded in
`docs/MIGRATION.md`.

## Wheel contents

The wheel must contain:

- every Python module under `src/karox`;
- `tui/theme.tcss`;
- `prompts/*.md`;
- `builtin_skills/*/SKILL.md`;
- `engine/py.typed` and its license/notice files;
- `LICENSE`.

There is no packaged `prompts/profiles/` source in this line. The bridge's
saved launch profiles are runtime JSON (`web-bridge-profiles.json`), not prompt
files. Do not require a `prompts/profiles/*.md` directory that the package does
not ship; `tests/test_package_data.py` checks the resources that do exist, so a
documentation edit cannot weaken them silently.

It must not contain `__pycache__`, `.pyc`, tests or an old
`tui_connections.py`/`hosted_bridge.py`. The release bridge modules are
`hosted_core.py` and `bridge_*.py`; those are expected. The engine's optional
extras (its own `run/` CLI needs typer, `agents/interactive` needs
prompt_toolkit, the portkey models need portkey-ai) are deliberately not
declared dependencies: the product path never imports them, and they fail with
their own instructive errors if something does.

## Release checklist

1. Inspect `git diff` and the final tree.
2. Run the full gate above.
3. Build the wheel and install it into a fresh virtual environment; run
   `karox --version`, `karox paths`, `karox provider presets`.
4. Run the live smoke through both the CLI and the terminal client.
5. Run `karox bridge connect ... --diagnostics-only` and, when a real connector
   is in scope, the OAuth/`tools/call` pass; never reset a foreign Funnel route.
6. Run `scripts/check_tree.py` on the packaged source.
7. Update `docs/CHANGELOG.md` and this file if a command or gate changed.
8. State any external action still required. Do not push, publish, tag or
   deploy without the owner's exact one-shot approval.
