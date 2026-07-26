#!/usr/bin/env python3
"""Fail when the repository's version numbers disagree with each other.

Three places used to state a version by hand and nothing compared them:
``VERSION`` said ``4.1.4``, ``pyproject.toml`` said ``5.0.0.dev0``, and the
quality workflow asserted a third hardcoded literal. Two of them happened to
agree on the day they were written, which is exactly how the third one silently
went stale.

There are genuinely two versioned artefacts in this tree, so there are two
sources of truth and no more:

* the **runtime package** -- ``src/karox/__init__.py`` ``__version__``.
  ``pyproject.toml`` derives the wheel version from it through setuptools
  ``dynamic``, so a wheel can no longer disagree with ``karox --version``.
* the **shipping release line** -- ``VERSION``, which ``release.yml`` turns into
  a tag and mirrors into ``server/repo_tools.py``.

They are different numbers on purpose: the published product is 4.x while the
vNext runtime is an unreleased 5.0 development version. This check makes that
deliberate rather than accidental, and it refuses any *new* hardcoded copy.

Runs on the standard library alone so it can execute before anything is
installed. ``--print runtime`` / ``--print release`` emit one value for CI to
consume instead of repeating it in YAML.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _package_version() -> str:
    """Read ``__version__`` without importing karox or its dependencies."""
    source = (ROOT / "src" / "karox" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
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
    # The comment above the declaration is the only thing telling a reader where
    # the number lives, and a stale comment is how this drift started.
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
    # RELEASE.json records what was actually published. It may legitimately lag
    # VERSION while a release is in flight, but it must never run ahead of it.
    #
    # This was written as an equality test, which contradicted the sentence above
    # and made the check fail on exactly the commit it is meant to guard: the one
    # that bumps VERSION. release.yml writes `"status": "published"` into every
    # marker, so the status was no escape either, and the new
    # tests-before-publish gate could never have gone green.
    recorded = marker.get("version")
    if marker.get("status") == "published" and isinstance(recorded, str):
        if _version_key(recorded) > _version_key(release):
            problems.append(
                f"RELEASE.json records published {recorded!r} which is ahead of "
                f"VERSION {release!r}; a marker may lag a release in flight, never lead it"
            )


def _version_key(text: str) -> tuple[int, ...]:
    """Order two release lines by their leading numeric components.

    Deliberately not a PEP 440 parser: this compares release lines such as 4.1.4,
    and the standard library offers nothing to do it with. A component with no
    digits sorts as 0 rather than raising, so a malformed marker cannot turn an
    ordering question into a crash.
    """
    components: list[int] = []
    for component in text.strip().split("."):
        digits = re.match(r"\d+", component)
        components.append(int(digits.group(0)) if digits else 0)
    return tuple(components)


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

    if problems:
        print("version sources disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        f"version sources agree (runtime {runtime} from src/karox/__init__.py, "
        f"release {release} from VERSION)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
