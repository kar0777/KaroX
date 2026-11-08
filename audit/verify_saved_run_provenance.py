"""Read-only proof that a saved agent workspace matches its original run ledger.

Used BEFORE external Windows browser regrading. Works with WSL-created git
worktrees copied to Windows, without relying on Windows core.autocrlf=TRUE
working-tree diff (which can show false modifications for unchanged LF files).
Never executes agent code, tests, shell hooks, model clients or network requests.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ARMS = ("karox5", "opencode_v2", "deepseek_harness")


class EvidenceIntegrityError(RuntimeError):
    """A saved result cannot be trusted for comparative scoring."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise EvidenceIntegrityError(reason)


def git(cwd: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        timeout=25,
    )
    require(result.returncode == 0, f"git {' '.join(arguments)} did not complete")
    return result.stdout


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if file.is_file():
            digest.update(file.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(file.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def git_blob_hash(data: bytes, *, algorithm: str = "sha1") -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    if algorithm == "sha1":
        return hashlib.sha1(header + data).hexdigest()
    if algorithm == "sha256":
        return hashlib.sha256(header + data).hexdigest()
    raise EvidenceIntegrityError("Unsupported Git object hash algorithm")


def git_tree_entries(data: bytes, *, staged: bool) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for raw in data.split(b"\0"):
        if not raw:
            continue
        require(b"\t" in raw, "Malformed Git tree entry")
        meta, filename = raw.split(b"\t", 1)
        parts = meta.decode("ascii").split()
        if staged:
            require(len(parts) == 3 and parts[2] == "0", "Merge conflict in saved agent index")
            mode, object_id = parts[:2]
        else:
            require(len(parts) == 3 and parts[1] == "blob", "Non-blob in frozen task base")
            mode, object_id = parts[0], parts[2]
        require(mode in ("100644", "100755"), "Unsafe symlink or unsupported Git file mode")
        path = filename.decode("utf-8")
        require(path not in entries, "Duplicated Git path")
        entries[path] = (mode, object_id)
    return entries


def verify(run_folder: Path, task_folder: Path) -> dict:
    run_folder = run_folder.resolve()
    task_folder = task_folder.resolve()
    require(run_folder.name in ARMS, "A competitor outside the exact three official arms")
    require(task_folder.is_dir(), "Task folder missing")
    workspace = run_folder / "workspace"
    raw = run_folder / "result.json"
    patch = run_folder / "changes.patch"
    manifest = task_folder / "task.json"
    fixture = task_folder / "fixture"
    grader = task_folder / "grader"
    require(
        raw.is_file() and patch.is_file() and (workspace / ".git").is_dir()
        and fixture.is_dir() and grader.is_dir() and manifest.is_file(),
        "Original result, staged patch, repository or task missing",
    )
    require(not workspace.is_symlink(), "Workspace is a symlink")
    task = json.loads(manifest.read_text(encoding="utf-8"))
    row = json.loads(raw.read_text(encoding="utf-8"))
    require(row.get("arm") == run_folder.name, "Raw arm name differs from run directory")
    require(row.get("task_id") == task["task_id"], "Raw task id differs from frozen task")
    require(row.get("task_sha256") == file_hash(manifest), "Task prompt hash differs")
    require(row.get("fixture_sha256") == tree_hash(fixture), "Frozen fixture tree mismatch")
    require(row.get("grader_sha256") == tree_hash(grader), "Frozen grader tree mismatch")
    recorded_patch = patch.read_bytes()
    require(
        type(row.get("patch_bytes")) is int and len(recorded_patch) == row["patch_bytes"],
        "Saved patch byte count differs from raw result",
    )
    require(
        row.get("patch_sha256") == hashlib.sha256(recorded_patch).hexdigest(),
        "Saved patch SHA-256 differs from raw result",
    )
    root = Path(git(workspace, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    require(root.resolve() == workspace, "Saved workspace is not Git repository root")

    algorithm = git(workspace, "rev-parse", "--show-object-format").decode().strip()
    require(algorithm in ("sha1", "sha256"), "Git repository has unknown object format")
    original = git_tree_entries(
        git(workspace, "ls-tree", "-r", "-z", "HEAD"), staged=False
    )
    staged = git_tree_entries(
        git(workspace, "ls-files", "--stage", "-z"), staged=True
    )
    frozen_files = {
        f.relative_to(fixture).as_posix(): f
        for f in fixture.rglob("*") if f.is_file()
    }
    require(set(original) == set(frozen_files), "Frozen Git HEAD file list differs from task fixture")
    require(len(staged) <= 3000, "Unexpectedly large agent workspace")
    for name, file in frozen_files.items():
        require(
            original[name][1] == git_blob_hash(file.read_bytes(), algorithm=algorithm),
            f"Frozen Git HEAD content differs from task fixture: {name}",
        )
    for name, (_mode, blob_sha) in staged.items():
        source = workspace.joinpath(*Path(name).parts)
        require(source.is_file() and not source.is_symlink(), f"Stage path is missing or unsafe: {name}")
        require(
            git_blob_hash(source.read_bytes(), algorithm=algorithm) == blob_sha,
            f"Saved workspace differs from frozen Git staged output: {name}",
        )
    untracked = git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
    require(not untracked, "Unexpected untracked agent workspace output")
    staged_patch = git(workspace, "diff", "--cached", "--binary")
    require(staged_patch == recorded_patch, "Saved staged Git patch differs from run ledger")
    return {
        "valid": True,
        "arm": row["arm"],
        "task_id": row["task_id"],
        "tracked_files": len(staged),
        "baseline_files": len(original),
        "patch_sha256": hashlib.sha256(recorded_patch).hexdigest(),
        "patch_bytes": len(recorded_patch),
        "baseline_fixture_sha256": row["fixture_sha256"],
        "grader_sha256": row["grader_sha256"],
        "result_sha256": file_hash(raw),
        "note": "Git object byte hashes avoid Windows core.autocrlf false positives",
    }
