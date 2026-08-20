"""Paired isolated cold/warm inspect benchmark: sequential vs production hashing."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import statistics
import sys
from pathlib import Path
from types import MethodType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("karox_inspect_pair_bench", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sequential_digest(self, path: Path, *, status_code: str, relative: str) -> str:
    from karox.repo_context import _IGNORED_STATUS_COMPONENTS

    digest = hashlib.sha256()

    def update(marker: str) -> None:
        digest.update(marker.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")

    def ignored(value: str) -> bool:
        return any(
            part.lower() in _IGNORED_STATUS_COMPONENTS or part.lower().endswith(".egg-info")
            for part in Path(value).parts
        )

    def hash_file(file_path: Path) -> str:
        content = hashlib.sha256()
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                content.update(chunk)
        return content.hexdigest()

    def visit(current: Path, current_relative: str) -> None:
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError:
            update(f"{status_code}\0{current_relative}\0missing")
            return
        if current.is_symlink():
            try:
                target = os.readlink(current)
            except OSError:
                target = "unreadable"
            update(f"{status_code}\0{current_relative}\0symlink\0{target}")
            return
        if not current.is_dir():
            try:
                content_hash = hash_file(current)
            except OSError:
                content_hash = "unreadable"
            update(f"{status_code}\0{current_relative}\0file\0{metadata.st_size}\0{content_hash}")
            return
        update(f"{status_code}\0{current_relative}\0dir")
        try:
            entries = sorted(os.scandir(current), key=lambda item: os.path.normcase(item.name))
        except OSError:
            update(f"{status_code}\0{current_relative}\0unreadable")
            return
        for entry in entries:
            child = Path(entry.path)
            try:
                child_relative = child.relative_to(self.repository).as_posix()
            except ValueError:
                continue
            if ignored(child_relative):
                continue
            visit(child, child_relative)

    visit(path, relative)
    return digest.hexdigest()


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def test_paired_inspect_hash_end_to_end() -> None:
    module = _load_benchmark()
    case = module.CASES["01"]
    rows: dict[str, dict[str, list[float]]] = {
        "sequential": {"cold": [], "warm": [], "identity_cold": [], "identity_warm": []},
        "parallel": {"cold": [], "warm": [], "identity_cold": [], "identity_warm": []},
    }
    for _ in range(3):
        with module._runtime_fixture(case, noise_files=250) as fixture:
            fixture.context._content_tree_digest = MethodType(_sequential_digest, fixture.context)
            cold, warm = module._inspect_pair(fixture, case)
            rows["sequential"]["cold"].append(cold["wall_ms"])
            rows["sequential"]["warm"].append(warm["wall_ms"])
            rows["sequential"]["identity_cold"].append(cold["compact_content_identity_ms"])
            rows["sequential"]["identity_warm"].append(warm["compact_content_identity_ms"])
        with module._runtime_fixture(case, noise_files=250) as fixture:
            cold, warm = module._inspect_pair(fixture, case)
            rows["parallel"]["cold"].append(cold["wall_ms"])
            rows["parallel"]["warm"].append(warm["wall_ms"])
            rows["parallel"]["identity_cold"].append(cold["compact_content_identity_ms"])
            rows["parallel"]["identity_warm"].append(warm["compact_content_identity_ms"])

    summary = {
        strategy: {key: _median(values) for key, values in metrics.items()}
        for strategy, metrics in rows.items()
    }
    for key in ("cold", "warm", "identity_cold", "identity_warm"):
        baseline = summary["sequential"][key]
        candidate = summary["parallel"][key]
        summary["parallel"][f"{key}_change_pct"] = round(
            100.0 * (candidate / baseline - 1.0), 1
        ) if baseline else 0.0
    payload: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "repo-inspect-parallel-hash-paired",
        "samples_per_strategy": 3,
        "noise_files": 250,
        "summary": summary,
    }
    output = ROOT / "benchmarks" / "agent_throughput" / "latest_parallel_hash_paired.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("PARALLEL_HASH_PAIRED=" + json.dumps(payload, sort_keys=True))
