#!/usr/bin/env python3
"""Fail when repository version and release-contract sources disagree.

There are two versioned artefacts in this tree:

* the runtime package: ``src/karox/__init__.py`` ``__version__``;
* the shipping release line: ``VERSION`` mirrored into the legacy server.

The runtime can be an unreleased KaroX 5 development version while the stable
shipping line remains 4.x. This script makes that difference deliberate, checks
that the wheel derives its version from the runtime source, and runs the KaroX 5
product, profile, user-copy, documented-command, installer, release-workflow,
and release-hygiene contracts when their repository files are present.

Standard library only. ``--print runtime`` and ``--print release`` emit values
for CI without repeating literals in workflow YAML.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import runpy
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
ProblemCollector = Callable[[Path], list[str] | tuple[list[str], object]]


def _package_version() -> str:
    """Read ``__version__`` without importing karox or its dependencies."""
    source = (ROOT / "src" / "karox" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                return node.value.value
    raise SystemExit("src/karox/__init__.py has no literal __version__ assignment")


def _release_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _repo_tools_version() -> str | None:
    source = (ROOT / "server" / "repo_tools.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', source, re.M)
    return match.group(1) if match else None


def _check_pyproject(problems: list[str], runtime: str) -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"^\[project\]$(.*?)^\[", text, re.S | re.M)
    body = project.group(1) if project else ""
    if re.search(r'^version\s*=\s*"', body, re.M):
        problems.append(
            "pyproject.toml [project] states a version literal; declare "
            'dynamic = ["version"] and let setuptools read karox.__version__'
        )
    if 'dynamic = ["version"]' not in body:
        problems.append('pyproject.toml [project] must declare dynamic = ["version"]')
    if 'version = { attr = "karox.__version__" }' not in text:
        problems.append(
            "pyproject.toml [tool.setuptools.dynamic] must derive version from "
            'attr = "karox.__version__"'
        )
    if runtime not in text and "karox.__version__" not in text:
        problems.append("pyproject.toml no longer references the runtime version source")


def _check_workflows(problems: list[str], runtime: str, release: str) -> None:
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for label, value in (("runtime", runtime), ("release", release)):
            if value in text:
                problems.append(
                    f".github/workflows/{path.name} hardcodes the {label} version "
                    f"{value!r}; read it from the source of truth instead"
                )


def _check_release_records(problems: list[str], release: str) -> None:
    mirrored = _repo_tools_version()
    if mirrored != release:
        problems.append(
            f"server/repo_tools.py VERSION is {mirrored!r} but VERSION says {release!r}"
        )
    notes = ROOT / f"RELEASE_NOTES_v{release}.md"
    if not notes.exists():
        problems.append(f"VERSION is {release!r} but {notes.name} does not exist")
    try:
        marker = json.loads((ROOT / "RELEASE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"RELEASE.json is unreadable: {type(exc).__name__}")
        return

    recorded = marker.get("version")
    if marker.get("status") == "published" and isinstance(recorded, str):
        if _version_key(recorded) > _version_key(release):
            problems.append(
                f"RELEASE.json records published {recorded!r} which is ahead of "
                f"VERSION {release!r}; a marker may lag a release in flight, never lead it"
            )


def _check_preview_record(problems: list[str], runtime: str) -> None:
    path = ROOT / "PREVIEW.json"
    if not path.is_file():
        return
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"PREVIEW.json is unreadable: {type(exc).__name__}")
        return
    if marker.get("schema_version") != 1 or marker.get("channel") != "preview":
        problems.append("PREVIEW.json must declare schema_version 1 and channel 'preview'")
    if marker.get("version") != runtime:
        problems.append(
            f"PREVIEW.json version {marker.get('version')!r} does not match runtime {runtime!r}"
        )
    expected_tag = f"v{runtime}"
    if marker.get("tag") != expected_tag:
        problems.append(
            f"PREVIEW.json tag {marker.get('tag')!r} does not match {expected_tag!r}"
        )
    notes = ROOT / f"RELEASE_NOTES_{expected_tag}.md"
    if not notes.is_file():
        problems.append(f"PREVIEW.json points at {expected_tag!r} but {notes.name} is missing")


def _version_key(text: str) -> tuple[int, ...]:
    """Order release lines by their leading numeric components."""
    components: list[int] = []
    for component in text.strip().split("."):
        digits = re.match(r"\d+", component)
        components.append(int(digits.group(0)) if digits else 0)
    return tuple(components)


def _load_problem_collector(path: Path) -> ProblemCollector:
    namespace = runpy.run_path(str(path))
    collect = namespace.get("collect_problems")
    if not callable(collect):
        raise TypeError(f"{path} has no callable collect_problems")
    return collect


def _contract_problems(path: Path) -> list[str]:
    collect = _load_problem_collector(path)
    result = collect(ROOT)
    raw = result[0] if isinstance(result, tuple) else result
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise TypeError(f"{path} collect_problems returned an invalid problem list")
    return raw


def _check_optional_contract(
    problems: list[str],
    *,
    marker: Path,
    checker: Path,
    label: str,
) -> None:
    """Run a repository contract when its marker exists."""
    if not marker.is_file():
        return
    if not checker.is_file():
        problems.append(f"{marker.relative_to(ROOT)} exists but {checker.name} is missing")
        return
    try:
        contract_problems = _contract_problems(checker)
    except Exception as exc:
        problems.append(f"{label} contract crashed: {type(exc).__name__}: {exc}")
        return
    problems.extend(f"{label}: {problem}" for problem in contract_problems)


def _check_repository_contracts(problems: list[str]) -> None:
    contracts = (
        (
            ROOT / "docs" / "V5_RELEASE_SCOPE.md",
            ROOT / "scripts" / "check_v5_release.py",
            "KaroX 5",
        ),
        (
            ROOT / "src" / "karox" / "policy.py",
            ROOT / "scripts" / "check_access_profiles.py",
            "access profiles",
        ),
        (
            ROOT / "src" / "karox" / "cli.py",
            ROOT / "scripts" / "check_user_facing_copy.py",
            "user-facing copy",
        ),
        (
            ROOT / "src" / "karox" / "cli.py",
            ROOT / "scripts" / "check_documented_commands.py",
            "documented commands",
        ),
        (
            ROOT / "install.karox.sh",
            ROOT / "scripts" / "check_installer_preservation.py",
            "installer preservation",
        ),
        (
            ROOT / ".github" / "workflows" / "release.yml",
            ROOT / "scripts" / "check_release_workflow.py",
            "release workflow",
        ),
        (
            ROOT / "docs" / "RELEASE_CHECKLIST.md",
            ROOT / "scripts" / "check_release_hygiene.py",
            "release hygiene",
        ),
    )
    for marker, checker, label in contracts:
        _check_optional_contract(
            problems,
            marker=marker,
            checker=checker,
            label=label,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", choices=("runtime", "release"), dest="emit")
    args = parser.parse_args(argv)

    runtime = _package_version()
    release = _release_version()

    if args.emit == "runtime":
        print(runtime)
        return 0
    if args.emit == "release":
        print(release)
        return 0

    problems: list[str] = []
    if not runtime:
        problems.append("src/karox/__init__.py __version__ is empty")
    if not release:
        problems.append("VERSION is empty")
    _check_pyproject(problems, runtime)
    _check_workflows(problems, runtime, release)
    _check_release_records(problems, release)
    _check_preview_record(problems, runtime)
    _check_repository_contracts(problems)

    if problems:
        print("version or release sources disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        f"version sources agree (runtime {runtime} from src/karox/__init__.py, "
        f"release {release} from VERSION); release contracts are internally consistent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
