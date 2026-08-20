"""Explicit isolated benchmark for CompositeHostedBridge routing; not auto-discovered."""
from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge, HostedBridgeAccessDenied
from karox.models import AccessProfile
from karox.sessions import SessionStore

ROOT = Path(__file__).resolve().parents[1]


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 4)


def test_gpt_web_composite_routing_benchmark() -> None:
    with tempfile.TemporaryDirectory(prefix="karox-gpt-web-routing-") as tmp:
        root = Path(tmp)
        repository = root / "repo"
        initialize_git_repository(repository)
        (repository / "sample.txt").write_bytes(b"before\n")
        previous_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        try:
            sessions = SessionStore(root / "sessions")
            sessions.create(
                repository,
                "isolated GPT Web routing benchmark",
                AccessProfile.WORKSPACE_WRITE,
                session_id="routing-benchmark",
            )
            core = CoreToolBridge(
                repository,
                sessions,
                "routing-benchmark",
                ["karox.repo.read_file", "karox.git.status"],
            )
            composite = CompositeHostedBridge([core])
            target = "karox.repo.read_file"

            def legacy_owner() -> object:
                for runtime in composite._runtimes:
                    if any(item.name == target for item in runtime.descriptors()):
                        return runtime
                raise HostedBridgeAccessDenied(f"hosted tool is not exposed: {target}")

            cached_ms: list[float] = []
            legacy_ms: list[float] = []
            for _ in range(100):
                started = time.perf_counter()
                assert composite._owner(target) is core
                cached_ms.append((time.perf_counter() - started) * 1000)

                started = time.perf_counter()
                assert legacy_owner() is core
                legacy_ms.append((time.perf_counter() - started) * 1000)

            payload = {
                "schema_version": 1,
                "benchmark": "gpt-web-composite-routing",
                "isolation": {
                    "disposable_repository": True,
                    "live_bridge_touched": False,
                    "saved_profile_touched": False,
                },
                "samples": 100,
                "cached_owner_median_ms": _median(cached_ms),
                "legacy_owner_median_ms": _median(legacy_ms),
            }
            legacy = payload["legacy_owner_median_ms"]
            cached = payload["cached_owner_median_ms"]
            payload["median_reduction_pct"] = (
                round(100.0 * (1.0 - cached / legacy), 1) if legacy else 0.0
            )
            result_path = ROOT / "benchmarks" / "agent_throughput" / "latest_gpt_web_routing.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print("GPT_WEB_ROUTING=" + json.dumps(payload, sort_keys=True))
            assert cached < legacy
        finally:
            if previous_runtime is None:
                os.environ.pop("KAROX_RUNTIME_DIR", None)
            else:
                os.environ["KAROX_RUNTIME_DIR"] = previous_runtime
