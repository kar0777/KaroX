#!/usr/bin/env python3
"""Reject installer behavior that deletes legacy data needed for rollback.

KaroX 5 migration must be dry-run-first, reversible, and preserve the latest
working 4.x/RepoPilotBridge state through the documented rollback window. The
wrapper installers may migrate legacy metadata, but they must not recursively
delete legacy configuration, application, or runtime directories automatically.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SHELL_DELETION = re.compile(
    r"^\s*rm\s+-rf\b[^\n]*(?:LEGACY_CONFIG_DIR|LEGACY_RUNTIME_DIR|"
    r"LEGACY_APP_ROOT|LEGACY_INSTALL_DIR|RepoPilotBridge|repo-agent-bridge)",
    re.IGNORECASE | re.MULTILINE,
)
POWERSHELL_DELETION = re.compile(
    r"^\s*Remove-Item\b[^\n]*(?:legacyConfigDir|legacyRuntimeDir|"
    r"legacyAppRoot|legacyInstallDir|RepoPilotBridge|repo-agent-bridge)",
    re.IGNORECASE | re.MULTILINE,
)

WRAPPERS = {
    "install.karox.sh": SHELL_DELETION,
    "install.karox.ps1": POWERSHELL_DELETION,
}


def collect_problems(root: Path = ROOT) -> list[str]:
    problems: list[str] = []

    for relative, destructive in WRAPPERS.items():
        path = root / relative
        if not path.is_file():
            problems.append(f"installer wrapper is missing: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        matches = destructive.findall(text)
        if matches:
            problems.append(
                f"{relative} recursively deletes legacy rollback data: "
                + "; ".join(item.strip() for item in matches)
            )
        lowered = text.lower()
        if "migration" not in lowered:
            problems.append(f"{relative} has no explicit legacy migration path")
        if "preserved for rollback" not in lowered:
            problems.append(
                f"{relative} does not explicitly preserve legacy data for rollback"
            )

    migration = root / "docs" / "MIGRATION_V4_TO_V5.md"
    if not migration.is_file():
        problems.append("docs/MIGRATION_V4_TO_V5.md is missing")
    else:
        text = migration.read_text(encoding="utf-8").lower()
        for phrase in (
            "default to a dry run",
            "leave the source installation untouched",
            "preserve a way to launch the previous stable runtime",
        ):
            if phrase not in text:
                problems.append(
                    f"migration contract is missing rollback guarantee {phrase!r}"
                )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    problems = collect_problems(ROOT)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    elif problems:
        print("installer rollback-preservation contract failed:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("installer wrappers preserve legacy data for rollback")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
