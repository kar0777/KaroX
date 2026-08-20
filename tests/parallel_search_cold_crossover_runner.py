"""Explicit cold parallel in-process search crossover benchmark."""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_parallel_search_cold_crossover.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_parallel_search_crossover", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _files(root: Path) -> list[Path]:
    paths: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as iterator:
            entries = list(iterator)
        directories: list[Path] = []
        for entry in entries:
            if directory == root and entry.name.lower() in {".git", ".karox"}:
                continue
            path = Path(entry.path)
            if entry.is_symlink():
                raise RuntimeError("unexpected symlink in fixture")
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
            elif entry.is_file(follow_symlinks=False):
                paths.append(path)
        stack.extend(directories)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _parallel_scan(root: Path, query: str, max_file_bytes: int, workers: int) -> tuple[tuple[object, ...], ...]:
    matcher = re.compile(re.escape(query), re.IGNORECASE)
    paths = _files(root)

    def scan(path: Path) -> list[tuple[object, ...]]:
        try:
            if path.stat().st_size > max_file_bytes:
                return []
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        relative = path.relative_to(root).as_posix()
        return [
            (relative, number, line, False)
            for number, line in enumerate(text.splitlines(), start=1)
            if matcher.search(line)
        ]

    rows: list[tuple[object, ...]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for matches in executor.map(scan, paths):
            rows.extend(matches)
    return tuple(rows)


def _one(module, case, noise_files: int, *, workers: int | None) -> tuple[float, tuple[tuple[object, ...], ...]]:
    with module._runtime_fixture(case, noise_files=noise_files) as fixture:
        runtime = fixture.delegate.inner._core()
        started = time.perf_counter()
        if workers is None:
            with (
                mock.patch.object(runtime, "_search_ripgrep", return_value=None),
                mock.patch.object(runtime, "_search_small_tree", return_value=None),
            ):
                result = runtime._search({"query": case.search_query}, 30.0)
            rows = tuple(
                (item["path"], item["line"], item["text"], item["clipped"])
                for item in result.get("matches", [])
                if isinstance(item, dict)
            )
        else:
            rows = _parallel_scan(fixture.repository, case.search_query, runtime.MAX_FILE_BYTES, workers)
        return (time.perf_counter() - started) * 1000, rows


def test_parallel_search_cold_crossover() -> None:
    module = _load_module()
    case = module.CASES["01"]
    sizes: dict[str, object] = {}
    for noise_files in (100, 200, 350, 500):
        git_samples: list[float] = []
        parallel_samples: list[float] = []
        semantic = True
        for _ in range(3):
            git_ms, git_rows = _one(module, case, noise_files, workers=None)
            parallel_ms, parallel_rows = _one(module, case, noise_files, workers=8)
            git_samples.append(git_ms)
            parallel_samples.append(parallel_ms)
            semantic = semantic and git_rows == parallel_rows
        sizes[str(noise_files)] = {
            "git_grep_median_ms": round(statistics.median(git_samples), 3),
            "parallel_8_median_ms": round(statistics.median(parallel_samples), 3),
            "semantic_match": semantic,
            "git_samples_ms": [round(value, 3) for value in git_samples],
            "parallel_samples_ms": [round(value, 3) for value in parallel_samples],
        }
    OUTPUT.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "benchmark": "parallel-search-cold-crossover",
                "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
                "sizes": sizes,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
