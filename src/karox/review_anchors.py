"""Pre-existing error paths in the functions a run edited, named in the review.

datamodel-code-generator-3070 (v54, both attempts): the maintainer review
asked to probe "an existing raise or pragma: no cover branch in a function you
edited"; the model dismissed the item by assertion ("unchanged", "not our
task") while a partial mapping still reached ``raise RuntimeError(...)`` in
the very function it edited. Naming the exact lines turns a menu item into a
concrete obligation. Python files only; any failure yields no note.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Set

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
MAX_ANCHORS = 6


def changed_lines(diff: str) -> Dict[str, Set[int]]:
    """New-side line numbers each file's ``git diff -U0`` hunks touch."""

    lines: Dict[str, Set[int]] = {}
    current = ""
    for row in diff.splitlines():
        if row.startswith("+++ "):
            current = row[6:] if row.startswith("+++ b/") else ""
            continue
        match = _HUNK.match(row)
        if match and current:
            start, count = int(match.group(1)), int(match.group(2) or "1")
            # A pure deletion (count 0) sits between start and start + 1.
            touched = range(start, start + count) if count else range(start, start + 2)
            lines.setdefault(current, set()).update(touched)
    return lines


def _is_test(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py") or "tests/" in path or path.startswith("test/")


def anchors(source: str, touched: Set[int]) -> List[str]:
    """Raises inside edited functions that the edit itself did not write."""

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: List[str] = []
    seen: Set[int] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(function, "end_lineno", None) or function.lineno
        if not any(function.lineno <= line <= end for line in touched):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Raise) or node.lineno in touched or node.lineno in seen:
                continue
            seen.add(node.lineno)
            text = ast.unparse(node)
            parent = parents.get(node)
            if isinstance(parent, ast.If) and node in parent.body:
                text = f"if {ast.unparse(parent.test)}: {text}"
            found.append(f"{node.lineno} `{text[:140]}`")
    return found


def review_note(repository: Path, commit: str) -> str:
    """The review's extra item, or "" when no edited function has an old raise."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "diff", "-U0", "--no-color", commit, "--", "*.py"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    items: List[str] = []
    for path, touched in sorted(changed_lines(result.stdout).items()):
        if _is_test(path):
            continue
        try:
            source = (Path(repository) / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        items += [f"{path}:{anchor}" for anchor in anchors(source, touched)]
    if not items:
        return ""
    listed = "; ".join(items[:MAX_ANCHORS])
    return (
        "\nError paths that already existed in the functions you edited: "
        f"{listed}. For each, run a probe with the inputs your change now lets "
        "reach that function (partial, missing or mixed values, not only the "
        "task's example) and print whether the path is hit; a hit the task's "
        "behaviour should cover is a defect to fix. An item is settled only by "
        "that output, never by reasoning that it is unchanged."
    )
