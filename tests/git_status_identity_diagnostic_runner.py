from __future__ import annotations

import json
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from _support import SRC, initialize_git_repository  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_git_status_identity_candidate.json"


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    ).stdout


def _status(repo: Path, *, no_renames: bool) -> bytes:
    args = ["status", "--porcelain=v1", "--untracked-files=normal", "-z"]
    if no_renames:
        args.append("--no-renames")
    return _git(repo, *args)


def _bench(repo: Path, *, no_renames: bool, repeats: int = 8) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        _status(repo, no_renames=no_renames)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def _paths(raw: bytes) -> set[str]:
    entries = raw.decode("utf-8", errors="replace").split("\0")
    result: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status = entry[:2]
        path = entry[3:]
        if status and path:
            result.add(path.replace("\\", "/"))
            if ("R" in status or "C" in status) and index < len(entries):
                old = entries[index]
                index += 1
                if old:
                    result.add(old.replace("\\", "/"))
    return result


def _fixture(mode: str):
    temporary = tempfile.TemporaryDirectory()
    repo = Path(temporary.name) / "repo"
    initialize_git_repository(repo)
    tracked = repo / "tracked.txt"
    tracked.write_bytes(b"before\n")
    for index in range(250):
        path = repo / "src" / f"file-{index:03d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=KaroX Test", "-c", "user.email=karox@example.invalid", "commit", "-m", "fixture")
    if mode == "dirty":
        tracked.write_bytes(b"after-with-different-size\n")
        scratch = repo / "scratch"
        scratch.mkdir()
        for index in range(120):
            (scratch / f"noise-{index:03d}.txt").write_text("x" * 1024, encoding="utf-8")
    elif mode == "rename":
        (repo / "tracked.txt").rename(repo / "renamed.txt")
        _git(repo, "add", "-A")
    return temporary, repo


def test_git_status_no_renames_candidate() -> None:
    results: dict[str, object] = {}
    for mode in ("clean", "dirty", "rename"):
        temporary, repo = _fixture(mode)
        try:
            default_raw = _status(repo, no_renames=False)
            candidate_raw = _status(repo, no_renames=True)
            default_ms = _bench(repo, no_renames=False)
            candidate_ms = _bench(repo, no_renames=True)
            default_paths = _paths(default_raw)
            candidate_paths = _paths(candidate_raw)
            # For rename mode no-renames intentionally expands one logical rename
            # into delete+add, but both old and new paths must remain represented.
            assert default_paths == candidate_paths
            results[mode] = {
                "default_median_ms": round(default_ms, 3),
                "no_renames_median_ms": round(candidate_ms, 3),
                "change_pct": round((candidate_ms - default_ms) / default_ms * 100, 1),
                "paths": sorted(default_paths),
            }
        finally:
            temporary.cleanup()
    payload = {
        "schema_version": 1,
        "benchmark": "git-status-fast-identity-no-renames",
        "results": results,
        "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
