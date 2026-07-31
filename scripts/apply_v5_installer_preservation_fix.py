#!/usr/bin/env python3
"""Remove automatic legacy-data deletion from KaroX installer wrappers.

KaroX 5 migration promises dry-run-first, reversible adoption. This one-time
helper is retained only for checkouts that still contain the old destructive
cleanup. It first runs the same preservation checker used by CI; when both
installers are already safe it exits successfully without changing a byte.

For an unsafe checkout the transformation is semantic rather than line-number
based:

- shell lines must be ``rm -rf`` commands that name a legacy variable/path;
- PowerShell lines must be ``Remove-Item`` commands that name a legacy
  variable/path;
- both wrappers are preflighted before either is changed;
- already-safe wrappers are left byte-identical;
- writes are atomic and all already-written files are restored on failure;
- a postcondition runs the same safety checker used by CI.

Remove this helper before strict/final release.
"""

from __future__ import annotations

import importlib.util
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "install.karox.sh"
POWERSHELL = ROOT / "install.karox.ps1"
CHECKER = ROOT / "scripts" / "check_installer_preservation.py"

SHELL_DELETION = re.compile(
    r"^(?P<indent>\s*)rm\s+-rf\b[^\n]*(?:LEGACY_CONFIG_DIR|LEGACY_RUNTIME_DIR|"
    r"LEGACY_APP_ROOT|LEGACY_INSTALL_DIR|RepoPilotBridge|repo-agent-bridge)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
POWERSHELL_DELETION = re.compile(
    r"^(?P<indent>\s*)Remove-Item\b[^\n]*(?:legacyConfigDir|legacyRuntimeDir|"
    r"legacyAppRoot|legacyInstallDir|RepoPilotBridge|repo-agent-bridge)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

SHELL_NOTICE = (
    'printf "%s\\n" "Legacy RepoPilotBridge data was preserved for rollback."'
)
POWERSHELL_NOTICE = (
    'Write-Host "  Legacy RepoPilotBridge data was preserved for rollback."'
)


@dataclass(frozen=True)
class FilePlan:
    path: Path
    original: str
    updated: str
    changed: bool


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_karox_installer_preservation_checker",
        CHECKER,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {CHECKER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collector() -> Callable[[Path], list[str]]:
    checker = _load_checker()
    collect = getattr(checker, "collect_problems", None)
    if not callable(collect):
        raise RuntimeError(f"{CHECKER} has no collect_problems")
    return collect


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _replace_destructive_lines(
    path: Path,
    pattern: re.Pattern[str],
    notice: str,
) -> FilePlan:
    original = path.read_text(encoding="utf-8")
    matches = list(pattern.finditer(original))
    if not matches:
        return FilePlan(path, original, original, False)

    first = True

    def replacement(match: re.Match[str]) -> str:
        nonlocal first
        indent = match.group("indent")
        if first:
            first = False
            return f"{indent}{notice}"
        if path.suffix.lower() == ".ps1":
            return f"{indent}# Legacy data intentionally preserved for rollback."
        return f"{indent}: # Legacy data intentionally preserved for rollback."

    updated = pattern.sub(replacement, original)
    if updated == original:
        raise RuntimeError(f"{path}: replacement produced no change")
    return FilePlan(path, original, updated, True)


def _restore(plans: list[FilePlan]) -> None:
    failures: list[str] = []
    for plan in reversed(plans):
        try:
            _atomic_write(plan.path, plan.original)
        except OSError as exc:
            failures.append(f"{plan.path}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit("rollback failed:\n  - " + "\n  - ".join(failures))


def main() -> int:
    collect = _collector()
    current_problems = collect(ROOT)
    if not current_problems:
        print("installer rollback-preservation fix is already applied")
        return 0

    plans = [
        _replace_destructive_lines(SHELL, SHELL_DELETION, SHELL_NOTICE),
        _replace_destructive_lines(
            POWERSHELL,
            POWERSHELL_DELETION,
            POWERSHELL_NOTICE,
        ),
    ]
    changed = [plan for plan in plans if plan.changed]
    if not changed:
        raise SystemExit(
            "installer preservation checker still fails, but no reviewed destructive "
            "line was found; inspect the reported problems before changing files:\n  - "
            + "\n  - ".join(current_problems)
        )

    written: list[FilePlan] = []
    try:
        for plan in changed:
            _atomic_write(plan.path, plan.updated)
            written.append(plan)
        problems = collect(ROOT)
        if problems:
            raise RuntimeError(
                "installer safety postcondition failed:\n  - "
                + "\n  - ".join(str(item) for item in problems)
            )
    except BaseException:
        _restore(written)
        raise

    changed_names = ", ".join(plan.path.name for plan in changed)
    print(f"legacy installer data is preserved for rollback in: {changed_names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
