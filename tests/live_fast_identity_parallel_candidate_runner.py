from __future__ import annotations

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import karox.repo_context as rc
from karox.artifacts import ArtifactStore
from karox.repo_context import RepositoryContextEngine

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_live_fast_identity_parallel_candidate.json"


def _work_items(context: RepositoryContextEngine, status: str) -> list[tuple[str, str, Path]]:
    items: list[tuple[str, str, Path]] = []
    for entry in status.split("\0"):
        if len(entry) < 4:
            continue
        status_code = entry[:2]
        raw_path = entry[3:]
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        relative = rc._safe_relative(context.repository, raw_path)
        if relative is None:
            continue
        if any(
            part.lower() in rc._IGNORED_STATUS_COMPONENTS
            or part.lower().endswith(".egg-info")
            for part in Path(relative).parts
        ):
            continue
        items.append((relative, status_code, context.repository / relative))
    return items


def _build(context: RepositoryContextEngine, revision: str, status: str, *, workers: int) -> dict[str, object]:
    items = _work_items(context, status)

    def digest(item: tuple[str, str, Path]) -> dict[str, str]:
        relative, status_code, path = item
        return {
            "path": relative,
            "sha256": context._metadata_tree_digest(path, status_code=status_code, relative=relative),
        }

    if workers > 1 and len(items) >= 8:
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            dirty = list(pool.map(digest, items))
    else:
        dirty = [digest(item) for item in items]
    dirty.sort(key=lambda item: item["path"])
    return {"revision": revision, "dirty": dirty}


def test_live_fast_identity_parallel_candidate() -> None:
    context = RepositoryContextEngine(ROOT, ArtifactStore("live-fast-identity-parallel"), policy_profile="workspace_write")
    revision, status = context._status_snapshot(untracked_files="normal")
    serial_fixed = _build(context, revision, status, workers=1)
    parallel_fixed = _build(context, revision, status, workers=16)
    assert parallel_fixed == serial_fixed

    samples: dict[str, list[float]] = {str(workers): [] for workers in (1, 4, 8, 16, 32)}
    for _ in range(3):
        for workers in (1, 4, 8, 16, 32):
            started = time.perf_counter()
            rev, snapshot = context._status_snapshot(untracked_files="normal")
            _build(context, rev, snapshot, workers=workers)
            samples[str(workers)].append((time.perf_counter() - started) * 1000)

    payload = {
        "schema_version": 1,
        "dirty_count": len(serial_fixed["dirty"]),
        "identity_equal": parallel_fixed == serial_fixed,
        "medians_ms": {
            workers: round(statistics.median(values), 3)
            for workers, values in samples.items()
        },
        "samples_ms": {
            workers: [round(value, 3) for value in values]
            for workers, values in samples.items()
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
