"""Repository-aware verification-command discovery.

Acceptance is not decided here.  ``completion_gate.floor`` and
``session_analysis`` own the proof that a check discharged an obligation; this
module only proposes exact, bounded argv for the repository the user selected.
The former ``VerificationResult``/``AutopilotPlan`` layer was an unused second
verification API and was removed in the deep refactor.
"""

from __future__ import annotations

import json
from pathlib import Path

from .python_tests import python_test_command
from .web_check import default_pages

#: ``npm`` scripts that are read-only enough to propose as verification.
_NPM_VERIFICATION_SCRIPTS: tuple[str, ...] = (
    "test",
    "verify",
    "typecheck",
    "type-check",
    "check:types",
    "lint",
    "format:check",
    "i18n:check",
    "build",
    "check:migrations",
    "check:encoding",
    "ci",
    "test:smoke",
)

# These markers are intentionally conservative. A hosted agent may run only the
# exact argv KaroX approves, but an npm script can hide a second command behind
# that argv. Refuse obvious mutation/publication recipes from automatic
# discovery; the user can still approve a custom command explicitly.
_RISKY_NPM_SCRIPT_MARKERS: tuple[str, ...] = (
    "--fix",
    "--write",
    " deploy",
    "deploy ",
    "publish",
    "prisma db push",
    "prisma migrate deploy",
    "supabase db push",
    "firebase deploy",
    "git push",
    "npm publish",
    "pnpm publish",
    "yarn publish",
    "rm -rf",
    "rimraf src",
)


def _safe_npm_verification_script(command: str) -> bool:
    normalized = f" {command.strip().lower()} "
    if not command.strip():
        return False
    return not any(marker in normalized for marker in _RISKY_NPM_SCRIPT_MARKERS)


def discover_verification_commands(repository: Path) -> tuple[tuple[str, ...], ...]:
    """Discover bounded verification argv for the selected repository.

    The result is deliberately project-aware and exact: only scripts that
    actually exist in ``package.json`` are returned, and scripts with obvious
    mutation/publication markers are excluded. This is the common source of
    truth for the TUI, direct hosted connectors, and saved-profile recovery.
    """

    repository = repository.expanduser().resolve()
    commands: list[tuple[str, ...]] = []
    package_json = repository / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if isinstance(scripts, dict):
            for name in _NPM_VERIFICATION_SCRIPTS:
                raw = scripts.get(name)
                if not isinstance(raw, str) or not _safe_npm_verification_script(raw):
                    continue
                command = ("npm", "test") if name == "test" else ("npm", "run", name)
                commands.append(command)

    python_command = python_test_command(repository)
    if python_command is not None and python_command not in commands:
        commands.append(python_command)
    if default_pages(repository):
        # A page deliverable needs its runtime proof even when project checks also
        # exist. Returning early after npm/pytest made the browser producer vanish,
        # leaving completion to choose between dead-ending and weakening proof.
        render_command = ("python", "-m", "karox.web_check", "--render")
        if render_command not in commands:
            commands.append(render_command)
    if commands:
        return tuple(commands)
    return (("git", "diff", "--check"),)


__all__ = ["discover_verification_commands"]
