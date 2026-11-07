"""Small output and argument helpers shared by CLI command groups.

These are presentation/parsing helpers, not agent runtime code.  Keeping them
out of ``cli.py`` lets command-group modules use the same output contract
without importing the entire command surface and creating a cycle.
"""

from __future__ import annotations

import json
import shlex
import sys
from typing import Any


def json_output_value(value: Any) -> None:
    write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write(text: str) -> None:
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding, "replace"))
    sys.stdout.flush()


def emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        json_output_value(value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            write(f"{key}: {item}\n")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                identifier = item.get("provider_id") or ""
                if item.get("model_id") is not None:
                    identifier = f"{identifier}/{item['model_id']}"
                write((identifier or json.dumps(item, ensure_ascii=False)) + "\n")
            else:
                write(f"{item}\n")
    else:
        write(f"{value}\n")


def verification_argv(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except ValueError:
        decoded = None
    if isinstance(decoded, list) and decoded and all(
        isinstance(item, str) and item for item in decoded
    ):
        return tuple(decoded)
    parts = tuple(shlex.split(value))
    if not parts:
        raise ValueError("a verification command must not be empty")
    return parts


__all__ = ["emit", "json_output_value", "verification_argv", "write"]
