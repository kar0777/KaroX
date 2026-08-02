#!/usr/bin/env python3
"""Verify that a built wheel matches the source tree.

A wheel is the artifact a user actually installs, so "the tests pass" says
nothing about it. Two failure modes are worth a gate of their own, and both
were observed on this repository:

**Stale modules.** ``setuptools`` copies sources into ``build/lib`` and then
zips *that* directory. It never removes files, so a module deleted from
``src/karox`` keeps living in ``build/lib`` and keeps shipping in every later
wheel. The installed package then contains code that no longer exists in the
repository, and a clean-checkout build produces a different artifact from the
same commit. This was real here: ``karox/markdown_render.py`` shipped in a
wheel built on 2026-08-02 while the module was absent from the source tree.

**Missing package data.** The Chrome extension is data, not Python, so a
packaging mistake drops it silently and the failure only appears later, on a
user's machine, as a browser feature that cannot start.

The check reads the newest wheel in ``dist`` with the standard library only, so
it runs anywhere the release does.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "karox"

# Files that must be inside the wheel for a browser feature to work at all.
REQUIRED_PACKAGE_DATA = (
    "karox/browser_extension/manifest.json",
    "karox/browser_extension/service_worker.js",
)


def newest_wheel(dist: Path) -> Path | None:
    wheels = sorted(
        (item for item in dist.glob("*.whl") if item.is_file()),
        key=lambda item: item.stat().st_mtime,
    )
    return wheels[-1] if wheels else None


def source_modules(root: Path) -> set[str]:
    package_root = root / "src" / PACKAGE
    return {
        item.relative_to(package_root).as_posix()
        for item in package_root.rglob("*.py")
    }


def wheel_modules(wheel: Path) -> set[str]:
    prefix = f"{PACKAGE}/"
    with zipfile.ZipFile(wheel) as archive:
        return {
            name[len(prefix) :]
            for name in archive.namelist()
            if name.startswith(prefix) and name.endswith(".py")
        }


def wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def collect_problems(root: Path, wheel: Path) -> list[str]:
    problems: list[str] = []

    in_source = source_modules(root)
    in_wheel = wheel_modules(wheel)

    for stale in sorted(in_wheel - in_source):
        problems.append(
            f"{wheel.name} ships {PACKAGE}/{stale}, which is not in src/{PACKAGE}; "
            "delete build/ and rebuild"
        )
    for missing in sorted(in_source - in_wheel):
        problems.append(
            f"{wheel.name} is missing {PACKAGE}/{missing}, which exists in source"
        )

    names = wheel_names(wheel)
    for required in REQUIRED_PACKAGE_DATA:
        if required not in names:
            problems.append(f"{wheel.name} is missing package data: {required}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        type=Path,
        help="wheel to inspect; defaults to the newest file in dist/",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args(argv)

    root = arguments.root.resolve()
    wheel = arguments.wheel or newest_wheel(root / "dist")
    if wheel is None:
        print("no wheel found in dist/; run python -m build --wheel first")
        return 1
    if not wheel.is_file():
        print(f"wheel not found: {wheel}")
        return 1

    problems = collect_problems(root, wheel)
    if problems:
        print(f"wheel contents do not match source: {wheel.name}")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"wheel matches source: {wheel.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
