"""One-shot launcher for a detached KaroX background command.

The parent places argv in a private runtime file so user objectives do not appear
in the operating-system process command line. This worker reads and deletes that
file before invoking the ordinary CLI entrypoint. It defines no second execution
path and receives no raw credential values.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .paths import runtime_dir

_SCHEMA_VERSION = 1


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="karox-background-worker")
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args(argv)
    allowed_root = (runtime_dir() / "vnext" / "background-orchestration").resolve()
    request = args.request_file.expanduser().resolve(strict=True)
    if not _inside(request, allowed_root):
        raise ValueError("background request file is outside the KaroX runtime directory")
    try:
        raw = json.loads(request.read_text(encoding="utf-8"))
    finally:
        try:
            request.unlink()
        except FileNotFoundError:
            pass
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("background request has unsupported schema")
    command = raw.get("argv")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ValueError("background request argv is invalid")
    from .cli import main as cli_main

    return int(cli_main(command) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
