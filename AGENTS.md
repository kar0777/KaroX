# KaroX release — agent guide

This repository is the clean KaroX 6 line. It keeps the verified local-agent
kernel and one compact terminal client. It is not the v5 laboratory: hosted
bridges, browser profiles, orchestration experiments and the legacy shell do
not belong here.

Source, tests and the release gates are authoritative when prose disagrees.

## Start every task

1. Confirm you are in `D:\проекты\karox-release`, not the v5 tree at
   `D:\проекты\KaroX-v5`.
2. Read `docs/ARCHITECTURE.md` before moving code between layers.
3. Preserve the working tree. Never reset, clean, or checkout unrelated work.
4. Prefer exact edits. Keep schemas, counts and command help synchronized.
5. Run the smallest affected check while editing; the release gates before
   declaring completion.

## Layout

- `src/karox/` — the product package. `cli.py`, `entrypoint.py` and `tui/` are
  the release surface written for this line. The remaining modules are the
  curated agent kernel copied from v5 (see `docs/MIGRATION.md`).
- `src/karox/prompts/`, `builtin_skills/`, `skill_notes/` — runtime data read
  from the installed package; they must travel in the wheel.
- `docs/` — current architecture and operator documentation. If a document
  disagrees with code, fix the document in the same change.
- `tests/` — release contracts. Tests must not import the parent v5 package:
  `tests/conftest.py` puts this package's `src/` first.

## Architecture rules

- The terminal client never imports the agent runtime. It spawns
  `karox agent run --stream-json` and reads one JSON document per line.
  Keep that boundary: it is what makes the UI testable without a model.
- The agent kernel talks to models only through `provider_adapters.py`; new
  provider behavior goes there and in `provider_factory.py`, not into the loop.
- Every mutating tool passes `policy.py` (capability) and `risk_engine.py`
  (consequence) before execution. Do not add a tool that bypasses either.
- Durable state lives under `paths.runtime_dir()`. Credentials never do; they
  are opaque OS-keyring references.
- MCP servers are discovered, selected per session, and called only through
  `mcp_client.py`. A selected tool's schema digest is re-checked at call time.
- `docs/ARCHITECTURE.md` documents the dependency direction. A new edge that
  points from the kernel back into the UI is a design error.

## Safety boundaries

- Local reads, normal edits, checks and local commits may proceed.
- Deleting source files, force-push, publishing, deployment, auth/account
  changes and payments stay behind an explicit user decision.
- Never print or persist raw credentials. Use a keyring reference or user
  takeover.
- `git push` is not part of the release workflow. It requires the owner's
  exact one-shot approval outside the agent loop.

## Verification

```text
python -m ruff check src tests
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python -m pytest
python -m build --wheel
python scripts/check_tree.py
```

The live smoke (provider + model configured, gateway reachable) is:

```text
karox agent run --repository <tmp> --task "create hello.txt with hi" \
  --verification-command '["python","-m","pytest","-q"]' --stream-json
```

Never call a run verified unless the report says `verified: true` and the
changed file or check actually proves it.

## Editing discipline

- Prefer exact string edits or atomic patches.
- Any new hosted mutation needs capability mapping, risk mapping, idempotency
  and tests for denied/tampered/replayed paths.
- Any transport or restart change must prove response completion, no duplicate
  side effect, owner identity and recovery after disconnect.
- Generated caches (`__pycache__`, `.pytest_cache`, build artifacts) must never
  be copied into the release tree. `scripts/check_tree.py` fails on them.
- Update `docs/CHANGELOG.md` when user-visible behavior changes.

## Finish

Before reporting completion: inspect the diff, run ruff, mypy on the release
surface, the full test suite, `scripts/check_tree.py`, build the wheel and
confirm its contents. State any external approval still required. Do not claim
release-ready while a deterministic gate is red.