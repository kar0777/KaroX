"""Isolated KaroX agent-throughput benchmark.

Measures production KaroX classes against disposable RelayDesk fixtures borrowed
from the real paired ChatGPT benchmark.  It never starts, restarts, or mutates a
saved bridge profile and never uses the live repository as a benchmark fixture.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import runpy
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
PAIRED_BENCHMARK = ROOT / "benchmarks" / "gpt_web_autonomy" / "real_chatgpt_benchmark.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from karox.artifacts import ArtifactStore
from karox.hosted_bridge import CoreToolBridge
from karox.models import AccessProfile
from karox.plan_executor import PlanExecutor
from karox.repo_context import RepositoryContextEngine
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import TaskStateStore

SCHEMA_VERSION = 1
READ_TOOLS = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
)


@dataclass(frozen=True)
class Case:
    task_id: str
    title: str
    goal: str
    search_query: str
    read_paths: tuple[str, ...]


CASES: dict[str, Case] = {
    "01": Case(
        "01",
        "Repository orientation",
        "normalize_connector_name implementation and directly related tests",
        "normalize_connector_name",
        ("src/relaydesk/validation.py", "tests/test_validation.py"),
    ),
    "02": Case(
        "02",
        "Call-flow investigation",
        "ConnectScreen submit validation ConnectionService RuntimeSupervisor WorkerRuntime flow",
        "ConnectScreen",
        (
            "src/relaydesk/tui.py",
            "src/relaydesk/service.py",
            "src/relaydesk/runtime.py",
            "src/relaydesk/validation.py",
        ),
    ),
    "03": Case(
        "03",
        "Bug diagnosis",
        "lease_expired boundary bug implementation and focused failing test",
        "lease_expired",
        ("src/relaydesk/lease.py", "tests/test_lease.py"),
    ),
    "05": Case(
        "05",
        "Multi-file change orientation",
        "ConnectionRequest model service runtime supervisor worker timeout propagation",
        "ConnectionRequest",
        (
            "src/relaydesk/models.py",
            "src/relaydesk/service.py",
            "src/relaydesk/runtime.py",
            "tests/test_service.py",
            "tests/test_runtime.py",
        ),
    ),
    "06": Case(
        "06",
        "Medium refactor orientation",
        "duplicated format_status implementation service runtime and direct tests",
        "format_status",
        (
            "src/relaydesk/service.py",
            "src/relaydesk/runtime.py",
            "tests/test_refactor.py",
        ),
    ),
}


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _paired_helpers() -> Mapping[str, Any]:
    return runpy.run_path(str(PAIRED_BENCHMARK), run_name="karox_agent_throughput_fixture")


def _write_fixture(repository: Path, task_id: str, *, noise_files: int) -> None:
    helpers = _paired_helpers()
    files = helpers["_base_fixture_files"](task_id=task_id)
    repository.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    helpers["_create_git_head"](repository)
    if noise_files > 0:
        noise_root = repository / "scratch-generated"
        noise_root.mkdir(parents=True, exist_ok=True)
        for index in range(noise_files):
            bucket = noise_root / f"bucket-{index % 16:02d}"
            bucket.mkdir(exist_ok=True)
            (bucket / f"item-{index:04d}.txt").write_text(
                f"generated-{index}\n", encoding="utf-8", newline="\n"
            )


class Timings:
    def __init__(self) -> None:
        self.values: dict[str, list[float]] = {}

    def add(self, name: str, milliseconds: float) -> None:
        self.values.setdefault(name, []).append(milliseconds)

    def total(self, name: str) -> float:
        return round(sum(self.values.get(name, ())), 3)

    def reset(self) -> None:
        self.values.clear()


class CountingDelegate:
    def __init__(self, inner: CoreToolBridge) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def descriptors(self) -> list[Any]:
        return self.inner.descriptors()

    def session_info(self) -> dict[str, Any]:
        return self.inner.session_info()

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        result = self.inner.execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )
        self.calls.append(
            {
                "tool": tool_name,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "result_bytes": _json_bytes(result),
            }
        )
        return result

    def reset(self) -> None:
        self.calls.clear()


@dataclass
class RuntimeFixture:
    root: Path
    repository: Path
    sessions: SessionStore
    artifacts: ArtifactStore
    context: RepositoryContextEngine
    delegate: CountingDelegate
    executor: PlanExecutor
    timings: Timings


def _instrument_method(obj: Any, name: str, timings: Timings, metric: str) -> None:
    original = getattr(obj, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            timings.add(metric, (time.perf_counter() - started) * 1000)

    setattr(obj, name, wrapped)


@contextlib.contextmanager
def _runtime_fixture(case: Case, *, noise_files: int) -> Any:
    with tempfile.TemporaryDirectory(prefix=f"karox-throughput-{case.task_id}-") as tmp:
        root = Path(tmp)
        repository = root / "repo"
        _write_fixture(repository, case.task_id, noise_files=noise_files)
        old_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        old_vnext = os.environ.get("KAROX_VNEXT_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime-vnext")
        executor: PlanExecutor | None = None
        try:
            sessions = SessionStore(root / "sessions")
            sessions.create(
                repository,
                f"Throughput benchmark task {case.task_id}",
                AccessProfile.WORKSPACE_WRITE,
                session_id="throughput-session",
            )
            artifacts = ArtifactStore("throughput-session")
            context = RepositoryContextEngine(
                repository,
                artifacts,
                policy_profile="workspace_write",
            )
            timings = Timings()
            _instrument_method(context, "_fast_revision_identity", timings, "fast_identity_ms")
            _instrument_method(context, "_revision_identity", timings, "strict_identity_ms")
            _instrument_method(
                context,
                "_compact_content_revision_identity",
                timings,
                "compact_content_identity_ms",
            )
            bridge = CoreToolBridge(
                repository,
                sessions,
                "throughput-session",
                list(READ_TOOLS),
                audit_path=root / "audit.jsonl",
            )
            delegate = CountingDelegate(bridge)
            executor = PlanExecutor(
                repository=repository,
                session_id="throughput-session",
                connection_id="throughput-benchmark",
                delegate=delegate,
                repo_context=context,
                task_states=TaskStateStore(sessions),
                artifacts=artifacts,
                lease_store=RepositoryLeaseStore(root / "repository-leases"),
                session_directory=sessions.session_dir("throughput-session"),
            )
            _instrument_method(executor.journals, "save", timings, "journal_save_ms")
            _instrument_method(executor.journals, "mutate", timings, "journal_mutate_ms")
            _instrument_method(artifacts, "put", timings, "artifact_put_ms")
            _instrument_method(executor, "_checkpoint_progress", timings, "checkpoint_ms")
            # Freshly-created directory metadata can settle asynchronously on
            # Windows. Benchmark only after the read-only identity is stable so
            # fixture creation is not misclassified as plan-induced drift.
            previous_identity = context._fast_revision_identity()
            for _ in range(20):
                time.sleep(0.025)
                current_identity = context._fast_revision_identity()
                if current_identity == previous_identity:
                    break
                previous_identity = current_identity
            else:
                raise RuntimeError("throughput fixture repository did not stabilize")
            timings.reset()
            yield RuntimeFixture(
                root=root,
                repository=repository,
                sessions=sessions,
                artifacts=artifacts,
                context=context,
                delegate=delegate,
                executor=executor,
                timings=timings,
            )
        finally:
            if executor is not None:
                executor.close()
            if old_runtime is None:
                os.environ.pop("KAROX_RUNTIME_DIR", None)
            else:
                os.environ["KAROX_RUNTIME_DIR"] = old_runtime
            if old_vnext is None:
                os.environ.pop("KAROX_VNEXT_RUNTIME_DIR", None)
            else:
                os.environ["KAROX_VNEXT_RUNTIME_DIR"] = old_vnext


def _manual_once(fixture: RuntimeFixture, case: Case) -> dict[str, Any]:
    fixture.delegate.reset()
    fixture.timings.reset()
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    results.append(fixture.delegate.execute("karox.repo.search", {"query": case.search_query}))
    for path in case.read_paths:
        results.append(fixture.delegate.execute("karox.repo.read_file", {"path": path}))
    wall_ms = (time.perf_counter() - started) * 1000
    return {
        "strategy": "manual_low_level",
        "wall_ms": round(wall_ms, 3),
        "conceptual_mcp_round_trips": len(results),
        "server_tool_calls": len(fixture.delegate.calls),
        "server_tool_ms": round(sum(item["duration_ms"] for item in fixture.delegate.calls), 3),
        "result_json_bytes": sum(_json_bytes(item) for item in results),
        "artifact_bytes": 0,
        "fast_identity_ms": fixture.timings.total("fast_identity_ms"),
        "strict_identity_ms": fixture.timings.total("strict_identity_ms"),
        "compact_content_identity_ms": fixture.timings.total("compact_content_identity_ms"),
        "journal_save_ms": fixture.timings.total("journal_save_ms"),
        "journal_mutate_ms": fixture.timings.total("journal_mutate_ms"),
        "artifact_put_ms": fixture.timings.total("artifact_put_ms"),
        "checkpoint_ms": fixture.timings.total("checkpoint_ms"),
    }


def _plan_once(fixture: RuntimeFixture, case: Case, iteration: int) -> dict[str, Any]:
    fixture.delegate.reset()
    fixture.timings.reset()
    operations: list[dict[str, Any]] = [
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
    started = time.perf_counter()
    result = fixture.executor.execute(
        arguments,
        f"throughput-{case.task_id}-{iteration}-{time.time_ns()}",
    )
    wall_ms = (time.perf_counter() - started) * 1000
    return {
        "strategy": "execute_plan",
        "wall_ms": round(wall_ms, 3),
        "conceptual_mcp_round_trips": 1,
        "server_tool_calls": len(fixture.delegate.calls),
        "server_tool_ms": round(sum(item["duration_ms"] for item in fixture.delegate.calls), 3),
        "result_json_bytes": _json_bytes(result),
        "artifact_bytes": int(result.get("total_size", 0) or 0),
        "fast_identity_ms": fixture.timings.total("fast_identity_ms"),
        "strict_identity_ms": fixture.timings.total("strict_identity_ms"),
        "compact_content_identity_ms": fixture.timings.total("compact_content_identity_ms"),
        "journal_save_ms": fixture.timings.total("journal_save_ms"),
        "journal_mutate_ms": fixture.timings.total("journal_mutate_ms"),
        "artifact_put_ms": fixture.timings.total("artifact_put_ms"),
        "checkpoint_ms": fixture.timings.total("checkpoint_ms"),
    }


def _inspect_pair(fixture: RuntimeFixture, case: Case) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture.delegate.reset()
    fixture.timings.reset()
    started = time.perf_counter()
    cold = fixture.context.inspect(case.goal, "focused")
    cold_wall = (time.perf_counter() - started) * 1000
    cold_metrics = {
        "strategy": "repo_inspect_cold",
        "wall_ms": round(cold_wall, 3),
        "conceptual_mcp_round_trips": 1,
        "server_tool_calls": 0,
        "server_tool_ms": 0.0,
        "result_json_bytes": _json_bytes(cold),
        "artifact_bytes": int(cold.get("total_size", 0) or 0),
        "fast_identity_ms": fixture.timings.total("fast_identity_ms"),
        "strict_identity_ms": fixture.timings.total("strict_identity_ms"),
        "compact_content_identity_ms": fixture.timings.total("compact_content_identity_ms"),
        "journal_save_ms": fixture.timings.total("journal_save_ms"),
        "journal_mutate_ms": fixture.timings.total("journal_mutate_ms"),
        "artifact_put_ms": fixture.timings.total("artifact_put_ms"),
        "checkpoint_ms": fixture.timings.total("checkpoint_ms"),
        "cache_hit": bool(cold.get("cache_hit")),
        "matches": int((cold.get("summary") or {}).get("matches", 0) or 0),
    }
    fixture.timings.reset()
    started = time.perf_counter()
    warm = fixture.context.inspect(case.goal, "focused")
    warm_wall = (time.perf_counter() - started) * 1000
    warm_metrics = {
        "strategy": "repo_inspect_warm",
        "wall_ms": round(warm_wall, 3),
        "conceptual_mcp_round_trips": 1,
        "server_tool_calls": 0,
        "server_tool_ms": 0.0,
        "result_json_bytes": _json_bytes(warm),
        "artifact_bytes": int(warm.get("total_size", 0) or 0),
        "fast_identity_ms": fixture.timings.total("fast_identity_ms"),
        "strict_identity_ms": fixture.timings.total("strict_identity_ms"),
        "compact_content_identity_ms": fixture.timings.total("compact_content_identity_ms"),
        "journal_save_ms": fixture.timings.total("journal_save_ms"),
        "journal_mutate_ms": fixture.timings.total("journal_mutate_ms"),
        "artifact_put_ms": fixture.timings.total("artifact_put_ms"),
        "checkpoint_ms": fixture.timings.total("checkpoint_ms"),
        "cache_hit": bool(warm.get("cache_hit")),
        "matches": int((warm.get("summary") or {}).get("matches", 0) or 0),
    }
    return cold_metrics, warm_metrics


def _median(rows: Sequence[Mapping[str, Any]], key: str) -> float:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return round(statistics.median(values), 3) if values else 0.0


def _summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["strategy"]), []).append(row)
    return {
        strategy: {
            "samples": len(items),
            "median_wall_ms": _median(items, "wall_ms"),
            "median_mcp_round_trips": _median(items, "conceptual_mcp_round_trips"),
            "median_server_tool_calls": _median(items, "server_tool_calls"),
            "median_server_tool_ms": _median(items, "server_tool_ms"),
            "median_result_json_bytes": _median(items, "result_json_bytes"),
            "median_artifact_bytes": _median(items, "artifact_bytes"),
            "median_fast_identity_ms": _median(items, "fast_identity_ms"),
            "median_strict_identity_ms": _median(items, "strict_identity_ms"),
            "median_compact_content_identity_ms": _median(
                items, "compact_content_identity_ms"
            ),
            "median_journal_save_ms": _median(items, "journal_save_ms"),
            "median_journal_mutate_ms": _median(items, "journal_mutate_ms"),
            "median_artifact_put_ms": _median(items, "artifact_put_ms"),
            "median_checkpoint_ms": _median(items, "checkpoint_ms"),
        }
        for strategy, items in sorted(grouped.items())
    }


def run_benchmark(
    *,
    case_ids: Sequence[str],
    repeats: int = 3,
    scenarios: Sequence[str] = ("clean", "dirty"),
    noise_files: int = 250,
) -> dict[str, Any]:
    if repeats < 1 or repeats > 20:
        raise ValueError("repeats must be between 1 and 20")
    unknown = set(case_ids).difference(CASES)
    if unknown:
        raise ValueError(f"unknown cases: {sorted(unknown)}")
    if set(scenarios).difference({"clean", "dirty"}):
        raise ValueError("scenarios must contain only clean and/or dirty")
    if noise_files < 0 or noise_files > 10_000:
        raise ValueError("noise_files must be between 0 and 10000")

    started = time.perf_counter()
    case_results: list[dict[str, Any]] = []
    for scenario in scenarios:
        scenario_noise = 0 if scenario == "clean" else noise_files
        for case_id in case_ids:
            case = CASES[case_id]
            rows: list[dict[str, Any]] = []
            for iteration in range(repeats):
                with _runtime_fixture(case, noise_files=scenario_noise) as fixture:
                    rows.append(_manual_once(fixture, case))
                with _runtime_fixture(case, noise_files=scenario_noise) as fixture:
                    rows.append(_plan_once(fixture, case, iteration))
                with _runtime_fixture(case, noise_files=scenario_noise) as fixture:
                    cold, warm = _inspect_pair(fixture, case)
                    rows.extend((cold, warm))
            summary = _summarize(rows)
            manual = summary["manual_low_level"]
            plan = summary["execute_plan"]
            inspect_cold = summary["repo_inspect_cold"]
            case_results.append(
                {
                    "task_id": case.task_id,
                    "title": case.title,
                    "scenario": scenario,
                    "noise_files": scenario_noise,
                    "samples": rows,
                    "summary": summary,
                    "derived": {
                        "plan_round_trip_reduction_pct": round(
                            100.0
                            * (1.0 - plan["median_mcp_round_trips"] / manual["median_mcp_round_trips"]),
                            1,
                        ),
                        "plan_server_wall_change_pct": round(
                            100.0
                            * (plan["median_wall_ms"] / manual["median_wall_ms"] - 1.0),
                            1,
                        )
                        if manual["median_wall_ms"]
                        else 0.0,
                        "inspect_cold_vs_manual_wall_change_pct": round(
                            100.0
                            * (inspect_cold["median_wall_ms"] / manual["median_wall_ms"] - 1.0),
                            1,
                        )
                        if manual["median_wall_ms"]
                        else 0.0,
                    },
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "karox-agent-throughput",
        "isolation": {
            "disposable_repositories": True,
            "live_bridge_touched": False,
            "saved_profiles_touched": False,
            "production_files_mutated_by_benchmark": False,
        },
        "configuration": {
            "case_ids": list(case_ids),
            "repeats": repeats,
            "scenarios": list(scenarios),
            "noise_files": noise_files,
        },
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "cases": case_results,
    }


def compact_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in payload.get("cases", []):
        summary = case["summary"]
        rows.append(
            {
                "task_id": case["task_id"],
                "scenario": case["scenario"],
                "manual_ms": summary["manual_low_level"]["median_wall_ms"],
                "plan_ms": summary["execute_plan"]["median_wall_ms"],
                "inspect_cold_ms": summary["repo_inspect_cold"]["median_wall_ms"],
                "inspect_warm_ms": summary["repo_inspect_warm"]["median_wall_ms"],
                "manual_round_trips": summary["manual_low_level"]["median_mcp_round_trips"],
                "plan_round_trips": summary["execute_plan"]["median_mcp_round_trips"],
                "plan_fast_identity_ms": summary["execute_plan"]["median_fast_identity_ms"],
                "plan_artifact_bytes": summary["execute_plan"]["median_artifact_bytes"],
                "inspect_artifact_bytes": summary["repo_inspect_cold"]["median_artifact_bytes"],
            }
        )
    return {
        "benchmark": payload.get("benchmark"),
        "duration_ms": payload.get("duration_ms"),
        "rows": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", dest="cases", choices=sorted(CASES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scenario", choices=("clean", "dirty", "both"), default="both")
    parser.add_argument("--noise-files", type=int, default=250)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    scenarios = ("clean", "dirty") if args.scenario == "both" else (args.scenario,)
    payload = run_benchmark(
        case_ids=args.cases or tuple(CASES),
        repeats=args.repeats,
        scenarios=scenarios,
        noise_files=args.noise_files,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    visible = compact_report(payload) if args.compact else payload
    print(json.dumps(visible, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
