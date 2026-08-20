"""Explicit isolated benchmark for a parallel content-tree hash candidate."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MethodType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("karox_parallel_hash_bench", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parallel_content_tree_digest(self, path: Path, *, status_code: str, relative: str) -> str:
    from karox.repo_context import _IGNORED_STATUS_COMPONENTS

    ordered: list[tuple[str, Any]] = []
    files: list[Path] = []

    def ignored(value: str) -> bool:
        return any(
            part.lower() in _IGNORED_STATUS_COMPONENTS or part.lower().endswith(".egg-info")
            for part in Path(value).parts
        )

    def visit(current: Path, current_relative: str) -> None:
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError:
            ordered.append(("marker", f"{status_code}\0{current_relative}\0missing"))
            return
        if current.is_symlink():
            try:
                target = os.readlink(current)
            except OSError:
                target = "unreadable"
            ordered.append(("marker", f"{status_code}\0{current_relative}\0symlink\0{target}"))
            return
        if not current.is_dir():
            index = len(files)
            files.append(current)
            ordered.append(("file", (status_code, current_relative, metadata.st_size, index)))
            return
        ordered.append(("marker", f"{status_code}\0{current_relative}\0dir"))
        try:
            entries = sorted(os.scandir(current), key=lambda item: os.path.normcase(item.name))
        except OSError:
            ordered.append(("marker", f"{status_code}\0{current_relative}\0unreadable"))
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

    def hash_file(file_path: Path) -> str:
        try:
            content = hashlib.sha256()
            with file_path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    content.update(chunk)
            return content.hexdigest()
        except OSError:
            return "unreadable"

    visit(path, relative)
    if len(files) <= 1:
        hashes = [hash_file(item) for item in files]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
            hashes = list(pool.map(hash_file, files))

    digest = hashlib.sha256()
    for kind, value in ordered:
        if kind == "file":
            code, file_relative, size, index = value
            marker = f"{code}\0{file_relative}\0file\0{size}\0{hashes[index]}"
        else:
            marker = value
        digest.update(marker.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def test_parallel_hash_candidate() -> None:
    module = _load_benchmark()
    case = module.CASES["01"]
    current_ms: list[float] = []
    parallel_ms: list[float] = []

    for _ in range(3):
        with module._runtime_fixture(case, noise_files=250) as fixture:
            started = time.perf_counter()
            fixture.context._compact_content_revision_identity()
            current_ms.append((time.perf_counter() - started) * 1000)
        with module._runtime_fixture(case, noise_files=250) as fixture:
            original = fixture.context._compact_content_revision_identity()
            fixture.context._content_tree_digest = MethodType(
                _parallel_content_tree_digest,
                fixture.context,
            )
            started = time.perf_counter()
            parallel = fixture.context._compact_content_revision_identity()
            parallel_ms.append((time.perf_counter() - started) * 1000)
            assert parallel == original

    payload = {
        "schema_version": 1,
        "benchmark": "repo-inspect-parallel-content-hash-candidate",
        "samples": 3,
        "noise_files": 250,
        "current_median_ms": round(statistics.median(current_ms), 3),
        "parallel_median_ms": round(statistics.median(parallel_ms), 3),
    }
    payload["change_pct"] = round(
        100.0 * (payload["parallel_median_ms"] / payload["current_median_ms"] - 1.0),
        1,
    )
    output = ROOT / "benchmarks" / "agent_throughput" / "latest_parallel_hash_candidate.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("PARALLEL_HASH_CANDIDATE=" + json.dumps(payload, sort_keys=True))
