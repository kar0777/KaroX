"""Explicit os.scandir search probe benchmark; not auto-discovered by pytest."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_scandir_search_probe.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_scandir_search_probe", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _walk_files(root: Path, limit: int) -> tuple[list[Path], bool]:
    files: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold(), reverse=True)
        except OSError:
            continue
        directories: list[Path] = []
        for entry in entries:
            if entry.name in {".git", ".karox"} and directory == root:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            files.append(Path(entry.path))
            if len(files) > limit:
                return files, True
        stack.extend(directories)
    return files, False


def _scan(root: Path, query: str, max_file_bytes: int, limit: int = 2000) -> dict[str, object]:
    matcher = re.compile(re.escape(query), re.IGNORECASE)
    paths, overflow = _walk_files(root, limit)
    matches: list[tuple[str, int, str]] = []
    scanned = 0
    skipped = 0
    for path in paths[:limit]:
        try:
            if path.stat().st_size > max_file_bytes:
                skipped += 1
                continue
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        scanned += 1
        relative = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                matches.append((relative, number, line))
    return {"matches": matches, "scanned": scanned, "skipped": skipped, "overflow": overflow}


def _median(callable_, repeats: int = 7) -> tuple[float, dict[str, object]]:
    values: list[float] = []
    last: dict[str, object] = {}
    for _ in range(repeats):
        started = time.perf_counter()
        last = callable_()
        values.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(values), 4), last


def test_scandir_search_probe() -> None:
    module = _load_module()
    case = module.CASES["01"]
    payload: dict[str, object] = {
        "schema_version": 1,
        "benchmark": "scandir-search-probe",
        "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
        "sizes": {},
    }
    for noise_files in (40, 250, 1000, 2000):
        with module._runtime_fixture(case, noise_files=noise_files) as fixture:
            runtime = fixture.delegate.inner._core()
            scandir_ms, result = _median(
                lambda: _scan(fixture.repository, case.search_query, runtime.MAX_FILE_BYTES)
            )
            probe_ms, probe = _median(
                lambda: {"walk": _walk_files(fixture.repository, 384)}
            )
            native_ms, native = _median(lambda: runtime._search({"query": case.search_query}, 30.0))
            native_semantic = sorted(
                (item["path"], item["line"], item["text"])
                for item in native.get("matches", [])
                if isinstance(item, dict)
            )
            payload["sizes"][str(noise_files)] = {
                "scandir_ms": scandir_ms,
                "probe_384_ms": probe_ms,
                "probe_384_overflow": bool(probe["walk"][1]),
                "native_ms": native_ms,
                "native_backend": native.get("backend"),
                "scandir_files": result["scanned"],
                "scandir_overflow": result["overflow"],
                "semantic_match_sorted": sorted(result["matches"]) == native_semantic,
            }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
