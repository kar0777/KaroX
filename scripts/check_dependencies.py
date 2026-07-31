#!/usr/bin/env python3
"""Fail when the declared dependencies do not match the code that imports them.

Two drifts have bitten this package. ``markdown-it-py`` was imported directly by
``src/karox/markdown_render.py`` and declared nowhere, so it arrived only because
``textual`` happened to depend on it -- an install that looks complete and then
fails at import time the moment that changes. And ``pyproject.toml`` and
``requirements.txt`` are maintained by hand as two copies of one list, so they
had already fallen out of order.

This check runs on the standard library alone so it can execute before anything
is installed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "karox"

# Distribution name -> the module name it actually installs, where they differ.
IMPORT_NAMES = {
    "markdown-it-py": "markdown_it",
    "httpx-sse": "httpx_sse",
    "PyYAML": "yaml",
    "Pygments": "pygments",
    "uvicorn[standard]": "uvicorn",
    "tomli": "tomli",
}

# Declared on purpose without a direct ``import`` in src/karox.
#   pydantic  - the legacy server/ runtime and the FastAPI/MCP request models
#   httpx-sse - required by the installed MCP SDK's HTTP transport
#   Pygments / markdown-it-py - the transcript renders answers with Textual's own
#               Markdown widget, which parses through markdown-it-py and
#               highlights fenced code through Pygments. KaroX imported both
#               directly until it had its own Rich markdown renderer; that
#               renderer was removed because a Rich renderable cannot be
#               selected, and these two are still required at runtime. They stay
#               declared rather than left to arrive through textual, which is the
#               exact drift the header of this file describes.
#   fastapi / uvicorn / tomli - imported lazily or only on one interpreter
INDIRECT_BUT_REQUIRED = {
    "pydantic",
    "httpx-sse",
    "Pygments",
    "markdown-it-py",
    "fastapi",
    "uvicorn[standard]",
    "tomli",
}

_REQUIREMENT = re.compile(r"^([A-Za-z0-9._-]+(?:\[[A-Za-z0-9,._-]+\])?)")


def _requirement_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _REQUIREMENT.match(stripped)
    return match.group(1) if match else None


def _pyproject_dependencies(text: str) -> list[str]:
    # Parsed with a regex rather than tomllib so this check also runs on 3.10,
    # which is the interpreter version the check most needs to protect.
    block = re.search(r"^dependencies = \[(.*?)^\]", text, re.S | re.M)
    if block is None:
        raise SystemExit("pyproject.toml has no dependencies list")
    return [
        name
        for line in block.group(1).splitlines()
        if (name := _requirement_name(line.strip().strip('",'))) is not None
    ]


def _pyproject_optional_dependencies(text: str) -> list[str]:
    """Requirements declared under ``[project.optional-dependencies]``.

    An extra satisfies an import without belonging in ``requirements.txt``: the
    runtime has to work when it is absent, which is why the importing code guards
    it. ``playwright`` is exactly that -- declared as the ``browser`` extra and
    imported inside the function that needs it -- and because this check read only
    the required list, it reported the extra as an undeclared dependency and
    failed the release.
    """
    block = re.search(
        r"^\[project\.optional-dependencies\](.*?)(?=^\[|\Z)", text, re.S | re.M
    )
    if block is None:
        return []
    names: list[str] = []
    for raw in re.findall(r"\[(.*?)\]", block.group(1), re.S):
        for item in raw.split(","):
            name = _requirement_name(item.strip().strip('"').strip("'"))
            if name is not None:
                names.append(name)
    return names


def _imported_top_level_modules() -> dict[str, set[str]]:
    local = {path.stem for path in PACKAGE.glob("*.py")} | {"karox"}
    stdlib = set(sys.stdlib_module_names)
    imports: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root in stdlib or root in local:
                    continue
                imports.setdefault(root, set()).add(path.name)
    return imports


def main() -> int:
    declared_project = _pyproject_dependencies(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared_requirements = [
        name
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if (name := _requirement_name(line)) is not None
    ]

    problems: list[str] = []

    only_project = set(declared_project) - set(declared_requirements)
    only_requirements = set(declared_requirements) - set(declared_project)
    for name in sorted(only_project):
        problems.append(f"{name} is in pyproject.toml but not requirements.txt")
    for name in sorted(only_requirements):
        problems.append(f"{name} is in requirements.txt but not pyproject.toml")

    declared_optional = _pyproject_optional_dependencies(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared_modules = {
        IMPORT_NAMES.get(name, name).replace("-", "_").lower()
        for name in (*declared_project, *declared_optional)
    }
    for module, users in sorted(_imported_top_level_modules().items()):
        if module.lower() not in declared_modules:
            problems.append(
                f"src/karox imports {module} (in {', '.join(sorted(users))}) "
                "but no dependency declares it"
            )

    imported = {module.lower() for module in _imported_top_level_modules()}
    for name in sorted(declared_project):
        if name in INDIRECT_BUT_REQUIRED:
            continue
        module = IMPORT_NAMES.get(name, name).replace("-", "_").lower()
        if module not in imported:
            problems.append(
                f"{name} is declared but nothing under src/karox imports it; "
                "remove it or add it to INDIRECT_BUT_REQUIRED with a reason"
            )

    if problems:
        print("dependency declarations are out of step:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"dependency declarations agree ({len(declared_project)} requirements)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
