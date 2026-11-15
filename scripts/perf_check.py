"""Resource gate: `karox --version` start time and idle RSS of the headless stack.

    python scripts/perf_check.py [--json]

Targets (DECISIONS.md D-008): start <= 300 ms (median of 7 runs after a warm-up),
idle RSS <= 80 MiB. Exit 1 when a target is missed. Override for slow machines with
KAROX_PERF_START_MS / KAROX_PERF_RSS_MB, never in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

START_MS = float(os.environ.get("KAROX_PERF_START_MS", "300"))
RSS_MB = float(os.environ.get("KAROX_PERF_RSS_MB", "80"))


def _karox_exe() -> list[str]:
    scripts = Path(sys.executable).parent
    for name in ("karox.exe", "karox"):
        candidate = scripts / name
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which("karox")
    if found:
        return [found]
    return [sys.executable, "-m", "karox6"]


def measure_start(runs: int = 7) -> float:
    cmd = [*_karox_exe(), "--version"]
    subprocess.run(cmd, check=True, capture_output=True)  # warm-up (pyc, disk cache)
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(cmd, check=True, capture_output=True)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def measure_rss() -> float:
    out = subprocess.run(
        [*_karox_exe(), "debug", "rss"], check=True, capture_output=True, text=True
    ).stdout
    return float(out.strip().splitlines()[-1])


def main() -> int:
    start = measure_start()
    rss = measure_rss()
    ok = start <= START_MS and 0 < rss <= RSS_MB
    report = {
        "start_ms": round(start, 1),
        "start_limit_ms": START_MS,
        "idle_rss_mb": round(rss, 1),
        "rss_limit_mb": RSS_MB,
        "ok": ok,
    }
    if "--json" in sys.argv:
        print(json.dumps(report))
    else:
        print(
            f"start {report['start_ms']} ms (<= {START_MS}), "
            f"idle RSS {report['idle_rss_mb']} MiB (<= {RSS_MB}): {'OK' if ok else 'FAIL'}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
