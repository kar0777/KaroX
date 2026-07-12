#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

TEXT_SUFFIXES = {".ps1", ".sh", ".py", ".cmd", ".bat", ".md", ".json", ".yml", ".yaml"}
SKIP_NAMES = {"karox_paths.py", "test_path_migration.py", "rebrand_runtime.py"}


def rewrite(root: Path) -> dict[str, object]:
    changed: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name in SKIP_NAMES or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            original = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        updated = original.replace("RepoPilotBridge", "KaroX")
        updated = updated.replace("scripts\\karox_cli.py", "scripts\\karox_admin_entry.py")
        updated = updated.replace("scripts/karox_cli.py", "scripts/karox_admin_entry.py")
        updated = updated.replace("scripts\\support_bundle.py", "scripts\\support_bundle_entry.py")
        updated = updated.replace("scripts/support_bundle.py", "scripts/support_bundle_entry.py")
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(str(path.relative_to(root)))
    return {"ok": True, "changed": changed, "changedCount": len(changed)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = rewrite(args.root.resolve())
    print(f"KaroX runtime rebrand: {result['changedCount']} file(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
