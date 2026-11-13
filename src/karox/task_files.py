"""Repository files the task names that the run never looked at.

A task often lists the files in scope ("possibly src/pkg/engine.py"). A model
that settles on a fix early can finish without ever opening one of them, and a
hidden check on that file then fails. The maintainer review is the last turn
before verification, so it names those files outright instead of trusting a
general "do every part of the task" rule the model has already read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, List, Mapping

_PATH = re.compile(
    r"(?<![\w/.:-])((?:[\w.-]+/)+[\w.-]+\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|rs|go|java|kt|rb|php"
    r"|c|h|cc|cpp|hpp|cs|swift|toml|cfg|ini|ya?ml|json|md|rst|html|css|scss|sh|sql))(?![\w/])"
)
_LIMIT = 6


def named_paths(task: str) -> List[str]:
    """Relative file paths written in the task text, in order, without repeats."""

    seen: List[str] = []
    for match in _PATH.finditer(task or ""):
        path = match.group(1).removeprefix("./")
        if path.startswith("/") or ".." in path.split("/") or path in seen:
            continue
        seen.append(path)
    return seen


def unopened(
    task: str,
    root: Path,
    messages: Iterable[Mapping[str, Any]],
    changed: Iterable[str] = (),
) -> List[str]:
    """Named files that exist in ``root`` but no tool call of the run mentioned."""

    candidates = []
    base = Path(root)
    for path in named_paths(task):
        try:
            if (base / path).is_file():
                candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return []
    touched = {str(p).replace("\\", "/") for p in changed}
    calls = []
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            try:
                calls.append(json.dumps(message["tool_calls"], ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                calls.append(str(message["tool_calls"]))
    text = "\n".join(calls).replace("\\\\", "/")
    return [p for p in candidates if p not in touched and p not in text][:_LIMIT]


def review_note(paths: List[str]) -> str:
    """The sentence appended to the maintainer review, or "" when nothing is owed."""

    if not paths:
        return ""
    listed = ", ".join(paths)
    return (
        f"\n7. The task names {listed}, and this run never opened "
        f"{'it' if len(paths) == 1 else 'them'}. Read each now and check the task's "
        "behaviour on it (for a speed or import-weight task, measure that module too); "
        "change it if the task's goal is not met there, before you finish."
    )
