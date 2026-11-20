"""Copy the changed files at the end of every run, so a finish can be graded later.

Layout: ``<session>/submissions/NN/files/<path>`` plus ``manifest.json`` with
``{"index", "step", "status", "files": {path: sha256 | None}, "skipped": [...]}``.
``None`` marks a file that the run deleted. Files that are too large, or that lie
outside the workspace, are listed under ``skipped`` instead of being copied.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024


def write_snapshot(
    session_dir: Path, workspace: Path, changed: list[str], *, step: int, status: str
) -> Path:
    root = session_dir / "submissions"
    root.mkdir(parents=True, exist_ok=True)
    index = 1 + sum(1 for p in root.iterdir() if p.is_dir() and p.name.isdigit())
    target = root / f"{index:02d}"
    files_dir = target / "files"
    files: dict[str, str | None] = {}
    skipped: list[str] = []
    total = 0
    for rel in changed:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or Path(rel).is_absolute():
            skipped.append(rel)
            continue
        src = workspace / rel
        if src.is_symlink() or not _inside(src, workspace):
            skipped.append(rel)  # a link could copy files from outside the project
            continue
        if not src.exists():
            files[rel] = None
            continue
        if not src.is_file():
            skipped.append(rel)
            continue
        size = src.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            skipped.append(rel)
            continue
        dst = files_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        total += size
        files[rel] = hashlib.sha256(dst.read_bytes()).hexdigest()
    manifest = {"index": index, "step": step, "status": status, "files": files}
    if skipped:
        manifest["skipped"] = skipped
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return target


def _inside(path: Path, workspace: Path) -> bool:
    """``path`` really lies in ``workspace`` (links and junctions in its parents resolved)."""
    try:
        return path.resolve().is_relative_to(workspace.resolve())
    except OSError:
        return False
