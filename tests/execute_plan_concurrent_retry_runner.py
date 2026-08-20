from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_execute_plan_concurrent_retry", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_concurrent_retry_same_plan_key_finishes_bounded() -> None:
    module = _load_module()
    case = module.CASES["01"]
    with module._runtime_fixture(case, noise_files=0) as fixture:
        operations = [
            {
                "operation_id": "search",
                "action": "search",
                "inputs": {"query": case.search_query},
                "output_policy": "summary",
            }
        ]
        operations.extend(
            {
                "operation_id": f"read-{index}",
                "action": "read",
                "inputs": {"path": path},
                "output_policy": "summary",
            }
            for index, path in enumerate(case.read_paths, start=1)
        )
        arguments = {"operations": operations, "stop_on_error": True}
        key = "concurrent-retry-same-plan"
        barrier = threading.Barrier(3)
        results: list[object] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=2.0)
                results.append(fixture.executor.execute(arguments, key))
            except BaseException as exc:  # diagnostic captures any failure
                errors.append(exc)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2.0)
        started = time.perf_counter()
        for thread in threads:
            thread.join(timeout=5.0)
        elapsed = time.perf_counter() - started

        alive = [thread.name for thread in threads if thread.is_alive()]
        assert not alive, f"concurrent retry hung after {elapsed:.3f}s: {alive}"
        assert not errors, repr(errors)
        assert len(results) == 2
        assert all(isinstance(item, dict) and item.get("ok") for item in results)
        assert any(bool(item.get("idempotent_replay")) for item in results if isinstance(item, dict))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
