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

## While editing

```text
python -m ruff check src tests
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python -m pytest tests/test_tui.py tests/test_cli.py
```

The copied kernel is not in mypy's file list yet. It is excused by scope, not by
`ignore_errors`; `follow_imports = "skip"` keeps mypy from checking files outside
the declared scope, and moving a module into the list is how that debt is paid
down. The suite currently collects 764 tests.

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

Bridge tests never open a public route. For a non-live local check:

```text
karox bridge connect chatgpt-web --repository . --tunnel custom \
  --public-url https://example.test --diagnostics-only --json
```

Do not run `bridge connect --tunnel tailscale` against a live connector as part
of a test: it creates a real Funnel route and a real keyring entry.

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
- `prompts/*.md` and `prompts/profiles/*.md`;
- `builtin_skills/*/SKILL.md`;
- `engine/py.typed` and its license/notice files;
- `LICENSE`.

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