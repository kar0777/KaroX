"""Print a JSON summary of the repository the session runs in.

Read-only by construction: it walks the working directory, skips dependency
and metadata trees, and prints counts. It writes nothing.
"""

import json
import os
import sys

SKIP = {
    ".git", ".karox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "__pycache__", "build", "dist", "node_modules",
    "target", "venv",
}


def main() -> int:
    root = os.environ.get("KAROX_REPOSITORY") or os.getcwd()
    if len(sys.argv) > 1:
        root = sys.argv[1]
    totals = {}
    largest = {}
    for current, directories, files in os.walk(root):
        directories[:] = sorted(d for d in directories if d not in SKIP)
        for name in sorted(files):
            path = os.path.join(current, name)
            try:
                with open(path, "rb") as handle:
                    size = len(handle.readlines())
            except OSError:
                continue
            extension = name.rsplit(".", 1)[-1] if "." in name else ""
            bucket = totals.setdefault(extension, [0, 0])
            bucket[0] += 1
            bucket[1] += size
            relative = os.path.relpath(current, root)
            top = largest.setdefault(extension, ["", 0])
            if size > top[1]:
                largest[extension] = [os.path.join(relative, name), size]
    print(
        json.dumps(
            {
                "root": root,
                "extensions": {
                    key: {"files": value[0], "lines": value[1]}
                    for key, value in sorted(totals.items())
                    if value[0] >= 3
                },
                "largest": {key: value for key, value in sorted(largest.items())},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
