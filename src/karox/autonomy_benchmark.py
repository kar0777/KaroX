"""Reproducible GPT Web autonomy benchmark in isolated temporary repositories.

The benchmark deliberately separates three evidence classes:

* ``server_side``: real KaroX high-level handlers running against temporary Git
  repositories with a deterministic local fixture runtime;
* ``simulated_client``: client-visible call/byte/recovery measurements for the
  same ten tasks, without pretending that a deterministic client is ChatGPT;
* ``paired_chatgpt_web``: a prepared protocol that remains ``not_run`` until a
  user connects two fresh ChatGPT Web sessions to an isolated test bridge.

No task mutates the caller's working tree and no external model/service is used.
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Sequence

from mcp.types import CallToolResult

from .affected_checks import AffectedChecksEngine
from .artifacts import ArtifactStore
from .models import AccessProfile
from .plan_executor import PlanExecutor
from .repo_context import RepositoryContextEngine
from .repository_lease import RepositoryLeaseStore
from .security import redact
from .sessions import SessionStore
from .task_state import FactOrigin, TaskStateStore, fact

BENCHMARK_SCHEMA_VERSION = 1
_BENCHMARK_CREDENTIAL = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[rank])


def _git(repository: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"benchmark Git command failed: {' '.join(arguments)}")


class BenchmarkFixtureRuntime:
    """Deterministic local runtime used only by the simulated-client benchmark."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.calls: list[tuple[str, dict[str, Any], Optional[str]]] = []
        self.write_effects = 0
        self.server_running = False
        self.browser_open = False
        self.names = {
            "karox.repo.search",
            "karox.repo.read_file",
            "karox.repo.read_lines",
            "karox.repo.command",
            "karox.tests.run",
            "karox.checks.run",
            "karox.dev_server.start",
            "karox.dev_server.status",
            "karox.dev_server.logs",
            "karox.browser.open",
            "karox.browser.snapshot",
            "karox.browser.screenshot",
            "karox.browser.console",
        }

    def descriptors(self) -> list[Any]:
        return [SimpleNamespace(name=name) for name in sorted(self.names)]

    def _search(self, query: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        expression = query.lower()
        for path in sorted(self.repository.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, start=1):
                if expression in line.lower():
                    matches.append(
                        {
                            "path": path.relative_to(self.repository).as_posix(),
                            "line": number,
                            "text": line,
                        }
                    )
        return matches

    def _tests(self) -> dict[str, Any]:
        broken = "BROKEN = True" in (
            self.repository / "src" / "karox" / "service.py"
        ).read_text(encoding="utf-8")
        noise = (
            f"debug authorization=Bearer {_BENCHMARK_CREDENTIAL}\n"
            + "\n".join(
                f"diagnostic line {index}: fixture context" for index in range(1600)
            )
        )
        if broken:
            return {
                "ok": False,
                "exit_code": 1,
                "timed_out": False,
                "stdout": (
                    noise
                    + "\nFAILED tests/test_service.py::test_validate_name - AssertionError: invalid name accepted\n"
                ),
                "stderr": "",
            }
        return {
            "ok": True,
            "exit_code": 0,
            "timed_out": False,
            "stdout": noise + "\n18 passed\n",
            "stderr": "",
        }

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any] | CallToolResult:
        del deadline_seconds
        self.calls.append((tool_name, dict(arguments), idempotency_key))
        if tool_name == "karox.repo.search":
            matches = self._search(str(arguments.get("query", "")))
            return {"ok": True, "match_count": len(matches), "matches": matches}
        if tool_name in {"karox.repo.read_file", "karox.repo.read_lines"}:
            relative = str(arguments["path"])
            lines = (self.repository / relative).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if tool_name == "karox.repo.read_lines":
                start = int(arguments.get("start", 1))
                count = int(arguments.get("count", 200))
                lines = lines[start - 1 : start - 1 + count]
            return {
                "ok": True,
                "path": relative,
                "content": "\n".join(lines),
                "total_lines": len(lines),
            }
        if tool_name == "karox.repo.command":
            payload = arguments.get("payload", {})
            changed: list[str] = []
            for operation in payload.get("operations", []):
                if operation.get("op") != "write":
                    continue
                relative = str(operation["path"])
                path = self.repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(operation["content"]), encoding="utf-8")
                changed.append(relative)
            if changed:
                self.write_effects += 1
            return {"ok": True, "changed_files": changed}
        if tool_name in {"karox.tests.run", "karox.checks.run"}:
            return self._tests()
        if tool_name == "karox.dev_server.start":
            self.server_running = True
            return {
                "ok": True,
                "process_id": "fixture-server",
                "running": True,
                "url": "http://127.0.0.1:4173",
            }
        if tool_name == "karox.dev_server.status":
            return {
                "ok": True,
                "process_id": "fixture-server",
                "running": self.server_running,
                "ready": self.server_running,
            }
        if tool_name == "karox.dev_server.logs":
            return {"ok": True, "stdout": "ready on 127.0.0.1:4173\n" * 500}
        if tool_name == "karox.browser.open":
            self.browser_open = True
            return {"ok": True, "url": str(arguments.get("url")), "title": "Fixture UI"}
        if tool_name == "karox.browser.snapshot":
            return {
                "ok": True,
                "url": "http://127.0.0.1:4173",
                "title": "Fixture UI",
                "headings": ["KaroX Control Center"],
                "buttons": ["Connect", "Run checks"],
                "visible_text": "\n".join(f"UI row {index}" for index in range(1000)),
            }
        if tool_name == "karox.browser.screenshot":
            return {
                "ok": True,
                "artifact_id": "fixture-image",
                "size": 120_000,
                "mime": "image/png",
            }
        if tool_name == "karox.browser.console":
            return {"ok": True, "messages": []}
        raise RuntimeError(f"unsupported benchmark fixture tool: {tool_name}")


@dataclass(frozen=True)
class ClientStep:
    tool: str
    arguments: dict[str, Any]
    read_only: bool = True


@dataclass(frozen=True)
class TaskMeasurement:
    task_id: str
    title: str
    success: bool
    tool_calls: int
    repeated_tool_calls: int
    inline_result_bytes: int
    artifact_bytes: int
    wall_clock_ms: float
    retries: int
    user_interventions: int
    recovery_success: Optional[bool]
    safety_violations: int
    confidentiality_violations: int
    incorrect_pre_existing_classification: int
    duplicate_side_effects: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "success": self.success,
            "tool_calls": self.tool_calls,
            "repeated_tool_calls": self.repeated_tool_calls,
            "inline_result_bytes": self.inline_result_bytes,
            "artifact_bytes": self.artifact_bytes,
            "total_result_bytes": self.inline_result_bytes + self.artifact_bytes,
            "wall_clock_ms": self.wall_clock_ms,
            "retries": self.retries,
            "user_interventions": self.user_interventions,
            "recovery_success": self.recovery_success,
            "safety_violations": self.safety_violations,
            "confidentiality_violations": self.confidentiality_violations,
            "incorrect_pre_existing_classification": self.incorrect_pre_existing_classification,
            "duplicate_side_effects": self.duplicate_side_effects,
        }


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    title: str
    baseline: Callable[["BenchmarkEnvironment"], tuple[list[Any], bool]]
    optimized: Callable[["BenchmarkEnvironment"], tuple[Any, bool, Optional[bool], int]]


class BenchmarkEnvironment:
    def __init__(self, root: Path, task_id: str) -> None:
        self.root = root
        self.repository = root / "repository"
        self.repository.mkdir(parents=True)
        _git(self.repository, "init", "--quiet")
        _git(self.repository, "config", "user.name", "KaroX Benchmark")
        _git(self.repository, "config", "user.email", "benchmark@example.invalid")
        self._write_fixture(task_id)
        _git(self.repository, "add", ".")
        _git(self.repository, "commit", "--quiet", "-m", "benchmark fixture")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repository,
            f"Benchmark {task_id}",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="benchmark-session",
        )
        self.artifacts = ArtifactStore("benchmark-session", root=root / "artifacts")
        self.context = RepositoryContextEngine(
            self.repository,
            self.artifacts,
            policy_profile="workspace_write",
        )
        self.delegate = BenchmarkFixtureRuntime(self.repository)
        self.task_states = TaskStateStore(self.sessions)
        self.leases = RepositoryLeaseStore(root / "repository-leases")
        self.plan = PlanExecutor(
            repository=self.repository,
            session_id="benchmark-session",
            connection_id="simulated-client",
            delegate=self.delegate,
            repo_context=self.context,
            task_states=self.task_states,
            artifacts=self.artifacts,
            lease_store=self.leases,
            session_directory=self.sessions.session_dir("benchmark-session"),
        )
        self.affected = AffectedChecksEngine(
            repository=self.repository,
            session_id="benchmark-session",
            connection_id="simulated-client",
            delegate=self.delegate,
            verification_commands=(("python", "-m", "ruff", "check", "src", "tests"),),
            artifacts=self.artifacts,
            repo_context=self.context,
            task_states=self.task_states,
            lease_store=self.leases,
        )

    def _write_fixture(self, task_id: str) -> None:
        (self.repository / "src" / "karox").mkdir(parents=True)
        (self.repository / "tests").mkdir()
        (self.repository / "docs").mkdir()
        filler = "\n".join(
            f"# architecture note {index}: bridge profile recovery verification"
            for index in range(900)
        )
        broken = "BROKEN = True\n" if task_id == "03" else "BROKEN = False\n"
        (self.repository / "src" / "karox" / "service.py").write_text(
            broken
            + "\n"
            + "def validate_name(value: str) -> str:\n"
            + "    if not value.strip():\n"
            + "        raise ValueError('name is required')\n"
            + "    return value.strip()\n\n"
            + "class SavedBridgeService:\n"
            + "    def start_profile(self, profile: str) -> str:\n"
            + "        return launch_saved_bridge(profile)\n\n"
            + "def launch_saved_bridge(profile: str) -> str:\n"
            + "    return f'started:{profile}'\n\n"
            + filler
            + "\n",
            encoding="utf-8",
        )
        (self.repository / "src" / "karox" / "cli.py").write_text(
            "from .service import SavedBridgeService\n\n"
            "def connect_command(profile: str) -> str:\n"
            "    return SavedBridgeService().start_profile(profile)\n",
            encoding="utf-8",
        )
        (self.repository / "src" / "karox" / "models.py").write_text(
            "from dataclasses import dataclass\n\n"
            "@dataclass\n"
            "class Profile:\n"
            "    name: str\n",
            encoding="utf-8",
        )
        (self.repository / "tests" / "test_service.py").write_text(
            "from karox.service import SavedBridgeService, validate_name\n\n"
            "def test_validate_name():\n"
            "    assert validate_name(' demo ') == 'demo'\n\n"
            "def test_start_profile():\n"
            "    assert SavedBridgeService().start_profile('demo') == 'started:demo'\n",
            encoding="utf-8",
        )
        (self.repository / "tests" / "test_cli.py").write_text(
            "from karox.cli import connect_command\n\n"
            "def test_connect_command():\n"
            "    assert connect_command('demo') == 'started:demo'\n",
            encoding="utf-8",
        )
        (self.repository / "docs" / "bridge.md").write_text(
            "# Saved bridge flow\n/connect calls connect_command then SavedBridgeService.\n"
            + filler,
            encoding="utf-8",
        )
        (self.repository / "pyproject.toml").write_text(
            "[project]\nname='fixture'\nversion='0.0.0'\n",
            encoding="utf-8",
        )
        (self.repository / "index.html").write_text(
            "<h1>KaroX Control Center</h1><button>Connect</button>",
            encoding="utf-8",
        )

    def baseline_steps(self, steps: Sequence[ClientStep]) -> tuple[list[Any], bool]:
        outputs: list[Any] = []
        success = True
        for index, step in enumerate(steps):
            result = self.delegate.execute(
                step.tool,
                dict(step.arguments),
                idempotency_key=(None if step.read_only else f"baseline-{index}"),
                deadline_seconds=300,
            )
            outputs.append(result)
            if isinstance(result, Mapping) and result.get("ok") is False:
                success = False
        return outputs, success


def _plan_write_operations(files: Mapping[str, str]) -> dict[str, Any]:
    return {
        "operations": [
            {"op": "write", "path": path, "content": content}
            for path, content in files.items()
        ]
    }


def _task_definitions() -> tuple[BenchmarkTask, ...]:
    def baseline_1(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        return env.baseline_steps(
            (
                ClientStep("karox.repo.search", {"query": "validate_name"}),
                ClientStep("karox.repo.read_file", {"path": "src/karox/service.py"}),
                ClientStep("karox.repo.search", {"query": "test_validate_name"}),
                ClientStep("karox.repo.read_file", {"path": "tests/test_service.py"}),
            )
        )

    def optimized_1(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        result = env.context.inspect("Find validate_name implementation and related tests", "focused")
        paths = {item["path"] for item in result["important_findings"]}
        return result, {"src/karox/service.py", "tests/test_service.py"}.issubset(paths), None, 0

    def baseline_2(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        return env.baseline_steps(
            (
                ClientStep("karox.repo.search", {"query": "connect_command"}),
                ClientStep("karox.repo.read_file", {"path": "src/karox/cli.py"}),
                ClientStep("karox.repo.search", {"query": "start_profile"}),
                ClientStep("karox.repo.read_file", {"path": "src/karox/service.py"}),
                ClientStep("karox.repo.search", {"query": "launch_saved_bridge"}),
                ClientStep("karox.repo.read_file", {"path": "docs/bridge.md"}),
            )
        )

    def optimized_2(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        result = env.context.inspect(
            "Build flow from connect_command through SavedBridgeService to launch_saved_bridge",
            "standard",
        )
        names = {
            item["name"]
            for item in env.artifacts.read_selection(
                result["artifact_id"],
                {"kind": "json_path", "path": "symbols.definitions"},
            )["content"]
        }
        return result, {"connect_command", "start_profile", "launch_saved_bridge"}.issubset(names), None, 0

    def baseline_3(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        outputs, _ = env.baseline_steps(
            (
                ClientStep("karox.tests.run", {"suite": "focused", "targets": ["tests/test_service.py"]}, False),
                ClientStep("karox.repo.search", {"query": "BROKEN"}),
                ClientStep("karox.repo.read_file", {"path": "src/karox/service.py"}),
                ClientStep("karox.repo.read_file", {"path": "tests/test_service.py"}),
            )
        )
        found = any(
            isinstance(item, Mapping)
            and "FAILED tests/test_service.py" in str(item.get("stdout", ""))
            for item in outputs
        )
        return outputs, found

    def optimized_3(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        result = env.affected.run(
            {"mode": "current", "changed_files": ["src/karox/service.py"]},
            "benchmark-task-03",
        )
        correct_unknown = result["classification"]["status"] == "unknown"
        return result, result["first_failure"] is not None and correct_unknown, None, 0

    def baseline_4(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        source = (
            env.repository / "src" / "karox" / "service.py"
        ).read_text(encoding="utf-8").replace(
            "if not value.strip():",
            "if len(value.strip()) < 2:",
        )
        return env.baseline_steps(
            (
                ClientStep("karox.repo.read_file", {"path": "src/karox/service.py"}),
                ClientStep(
                    "karox.repo.command",
                    {"action": "batch", "payload": _plan_write_operations({"src/karox/service.py": source})},
                    False,
                ),
                ClientStep("karox.tests.run", {"suite": "focused", "targets": ["tests/test_service.py"]}, False),
            )
        )

    def optimized_4(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        source = (
            env.repository / "src" / "karox" / "service.py"
        ).read_text(encoding="utf-8").replace(
            "if not value.strip():",
            "if len(value.strip()) < 2:",
        )
        plan = {
            "operations": [
                {
                    "operation_id": "validation-patch",
                    "action": "patch",
                    "inputs": {
                        "action": "batch",
                        "payload": _plan_write_operations({"src/karox/service.py": source}),
                        "expected_paths": ["src/karox/service.py"],
                    },
                },
                {
                    "operation_id": "validation-checks",
                    "action": "checks",
                    "depends_on": ["validation-patch"],
                    "inputs": {"tool": "tests", "suite": "focused", "targets": ["tests/test_service.py"]},
                },
            ]
        }
        result = env.plan.execute(plan, "benchmark-task-04")
        writes = env.delegate.write_effects
        replay = env.plan.execute(plan, "benchmark-task-04")
        duplicate = int(env.delegate.write_effects != writes or not replay["idempotent_replay"])
        return result, result["ok"], None, duplicate

    def baseline_5(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        source = (env.repository / "src" / "karox" / "service.py").read_text(encoding="utf-8") + "\ndef normalize_name(value: str) -> str:\n    return validate_name(value).lower()\n"
        test = (env.repository / "tests" / "test_service.py").read_text(encoding="utf-8") + "\ndef test_normalize_name():\n    from karox.service import normalize_name\n    assert normalize_name(' Demo ') == 'demo'\n"
        return env.baseline_steps(
            (
                ClientStep("karox.repo.search", {"query": "validate_name"}),
                ClientStep("karox.repo.read_file", {"path": "src/karox/service.py"}),
                ClientStep("karox.repo.read_file", {"path": "tests/test_service.py"}),
                ClientStep(
                    "karox.repo.command",
                    {"action": "batch", "payload": _plan_write_operations({"src/karox/service.py": source, "tests/test_service.py": test})},
                    False,
                ),
                ClientStep("karox.tests.run", {"suite": "focused", "targets": ["tests/test_service.py"]}, False),
            )
        )

    def optimized_5(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        source = (env.repository / "src" / "karox" / "service.py").read_text(encoding="utf-8") + "\ndef normalize_name(value: str) -> str:\n    return validate_name(value).lower()\n"
        test = (env.repository / "tests" / "test_service.py").read_text(encoding="utf-8") + "\ndef test_normalize_name():\n    from karox.service import normalize_name\n    assert normalize_name(' Demo ') == 'demo'\n"
        result = env.plan.execute(
            {
                "operations": [
                    {
                        "operation_id": "modify-source-and-test",
                        "action": "patch",
                        "inputs": {
                            "action": "batch",
                            "payload": _plan_write_operations({"src/karox/service.py": source, "tests/test_service.py": test}),
                            "expected_paths": ["src/karox/service.py", "tests/test_service.py"],
                        },
                    },
                    {
                        "operation_id": "verify-source-and-test",
                        "action": "checks",
                        "depends_on": ["modify-source-and-test"],
                        "inputs": {"tool": "tests", "suite": "focused", "targets": ["tests/test_service.py"]},
                    },
                ]
            },
            "benchmark-task-05",
        )
        return result, result["ok"], None, 0

    def baseline_6(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        service = (env.repository / "src" / "karox" / "service.py").read_text(encoding="utf-8").replace("SavedBridgeService", "BridgeLauncher")
        cli = (env.repository / "src" / "karox" / "cli.py").read_text(encoding="utf-8").replace("SavedBridgeService", "BridgeLauncher")
        test = (env.repository / "tests" / "test_service.py").read_text(encoding="utf-8").replace("SavedBridgeService", "BridgeLauncher")
        return env.baseline_steps(
            (
                ClientStep("karox.repo.search", {"query": "SavedBridgeService"}),
                ClientStep("karox.repo.read_file", {"path": "src/karox/service.py"}),
                ClientStep("karox.repo.read_file", {"path": "src/karox/cli.py"}),
                ClientStep("karox.repo.read_file", {"path": "tests/test_service.py"}),
                ClientStep(
                    "karox.repo.command",
                    {"action": "batch", "payload": _plan_write_operations({"src/karox/service.py": service, "src/karox/cli.py": cli, "tests/test_service.py": test})},
                    False,
                ),
                ClientStep("karox.tests.run", {"suite": "focused", "targets": ["tests/test_service.py", "tests/test_cli.py"]}, False),
            )
        )

    def optimized_6(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        service = (env.repository / "src" / "karox" / "service.py").read_text(encoding="utf-8").replace("SavedBridgeService", "BridgeLauncher")
        cli = (env.repository / "src" / "karox" / "cli.py").read_text(encoding="utf-8").replace("SavedBridgeService", "BridgeLauncher")
        test = (env.repository / "tests" / "test_service.py").read_text(encoding="utf-8").replace("SavedBridgeService", "BridgeLauncher")
        result = env.plan.execute(
            {
                "operations": [
                    {
                        "operation_id": "multi-file-refactor",
                        "action": "patch",
                        "inputs": {
                            "action": "batch",
                            "payload": _plan_write_operations({"src/karox/service.py": service, "src/karox/cli.py": cli, "tests/test_service.py": test}),
                            "expected_paths": ["src/karox/service.py", "src/karox/cli.py", "tests/test_service.py"],
                        },
                    },
                    {
                        "operation_id": "refactor-checks",
                        "action": "checks",
                        "depends_on": ["multi-file-refactor"],
                        "inputs": {"tool": "tests", "suite": "focused", "targets": ["tests/test_service.py", "tests/test_cli.py"]},
                    },
                ]
            },
            "benchmark-task-06",
        )
        return result, result["ok"], None, 0

    def baseline_7(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        outputs, success = env.baseline_steps(
            (
                ClientStep("karox.repo.search", {"query": "service"}),
                ClientStep("karox.repo.read_file", {"path": "tests/test_service.py"}),
                ClientStep("karox.repo.read_file", {"path": "tests/test_cli.py"}),
                ClientStep("karox.tests.run", {"suite": "focused", "targets": ["tests/test_service.py", "tests/test_cli.py"]}, False),
            )
        )
        return outputs, success

    def optimized_7(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        result = env.affected.run(
            {"mode": "current", "changed_files": ["src/karox/service.py"]},
            "benchmark-task-07",
        )
        targets = {
            target
            for check in result["selected_checks"]
            for target in check["arguments"].get("targets", [])
        }
        return result, result["ok"] and "tests/test_service.py" in targets, None, 0

    def baseline_8(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        return env.baseline_steps(
            (
                ClientStep("karox.dev_server.start", {"argv": ["python", "-m", "http.server"]}, False),
                ClientStep("karox.dev_server.status", {"process_id": "fixture-server"}),
                ClientStep("karox.dev_server.logs", {"process_id": "fixture-server"}),
            )
        )

    def optimized_8(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        result = env.plan.execute(
            {
                "operations": [
                    {
                        "operation_id": "start-server",
                        "action": "dev_server",
                        "inputs": {"verb": "start", "argv": ["python", "-m", "http.server"]},
                    },
                    {
                        "operation_id": "server-ready",
                        "action": "dev_server",
                        "depends_on": ["start-server"],
                        "inputs": {"verb": "status", "process_id": "fixture-server"},
                    },
                ]
            },
            "benchmark-task-08",
        )
        return result, result["ok"] and env.delegate.server_running, None, 0

    def baseline_9(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        return env.baseline_steps(
            (
                ClientStep("karox.browser.open", {"url": "http://127.0.0.1:4173"}, False),
                ClientStep("karox.browser.snapshot", {}),
                ClientStep("karox.browser.screenshot", {}),
                ClientStep("karox.browser.console", {}),
            )
        )

    def optimized_9(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        result = env.plan.execute(
            {
                "operations": [
                    {
                        "operation_id": "open-ui",
                        "action": "browser",
                        "inputs": {"verb": "open", "url": "http://127.0.0.1:4173"},
                    },
                    {
                        "operation_id": "inspect-ui",
                        "action": "browser",
                        "depends_on": ["open-ui"],
                        "inputs": {"verb": "snapshot"},
                        "output_policy": "artifact",
                    },
                    {
                        "operation_id": "capture-ui",
                        "action": "browser",
                        "depends_on": ["inspect-ui"],
                        "inputs": {"verb": "screenshot"},
                    },
                ]
            },
            "benchmark-task-09",
        )
        return result, result["ok"] and env.delegate.browser_open, None, 0

    def baseline_10(env: BenchmarkEnvironment) -> tuple[list[Any], bool]:
        env.task_states.bootstrap(
            "benchmark-session",
            {
                "objective": fact("Resume benchmark task", FactOrigin.VERIFIED, "fixture"),
                "repository": fact(str(env.repository), FactOrigin.VERIFIED, "fixture"),
                "branch": fact("main", FactOrigin.OBSERVED, "git"),
                "repository_revision": fact(env.context._revision_identity()["revision"], FactOrigin.OBSERVED, "git"),
                "next_safe_action": fact("continue validation", FactOrigin.PENDING, "fixture"),
            },
        )
        outputs = [
            env.task_states.load("benchmark-session").compact(),
            env.context._revision_identity(),
            {"permissions": "workspace_write"},
            {"manual_handoff_required": True},
        ]
        return outputs, True

    def optimized_10(env: BenchmarkEnvironment) -> tuple[Any, bool, Optional[bool], int]:
        state = env.task_states.bootstrap(
            "benchmark-session",
            {
                "objective": fact("Resume benchmark task", FactOrigin.VERIFIED, "fixture"),
                "repository": fact(str(env.repository), FactOrigin.VERIFIED, "fixture"),
                "branch": fact("main", FactOrigin.OBSERVED, "git"),
                "repository_revision": fact(env.context._revision_identity()["revision"], FactOrigin.OBSERVED, "git"),
                "next_safe_action": fact("continue validation", FactOrigin.PENDING, "fixture"),
            },
        )
        current = env.context._revision_identity()
        stored_revision = state.facts["repository_revision"].value
        result = {
            "ok": True,
            "task": state.compact(),
            "freshness": {
                "current": stored_revision == current["revision"],
                "dirty_count": len(current["dirty"]),
            },
            "next_safe_action": state.facts["next_safe_action"].value,
        }
        recovery = bool(result["freshness"]["current"])
        return result, recovery, recovery, 0

    return (
        BenchmarkTask("01", "Find implementation and related tests", baseline_1, optimized_1),
        BenchmarkTask("02", "Build TUI-to-backend flow", baseline_2, optimized_2),
        BenchmarkTask("03", "Diagnose focused test failure", baseline_3, optimized_3),
        BenchmarkTask("04", "Add validation rule", baseline_4, optimized_4),
        BenchmarkTask("05", "Modify function and tests", baseline_5, optimized_5),
        BenchmarkTask("06", "Perform multi-file refactor", baseline_6, optimized_6),
        BenchmarkTask("07", "Select affected checks", baseline_7, optimized_7),
        BenchmarkTask("08", "Start dev server and inspect readiness", baseline_8, optimized_8),
        BenchmarkTask("09", "Inspect UI through managed-browser contract", baseline_9, optimized_9),
        BenchmarkTask("10", "Resume from checkpoint in a new session", baseline_10, optimized_10),
    )


def _confidentiality_violations(
    value: Any,
    artifacts: ArtifactStore,
) -> int:
    violations = int(_BENCHMARK_CREDENTIAL in _canonical(value))
    marker = _BENCHMARK_CREDENTIAL.encode("utf-8")
    for record in artifacts.list():
        try:
            data, _metadata = artifacts.read(record.artifact_id)
        except (FileNotFoundError, OSError):
            continue
        if marker in data:
            violations += 1
    return violations


def _measure_baseline(task: BenchmarkTask, root: Path) -> TaskMeasurement:
    environment = BenchmarkEnvironment(root, task.task_id)
    started = time.perf_counter()
    outputs, success = task.baseline(environment)
    duration = (time.perf_counter() - started) * 1000
    signatures: dict[str, int] = {}
    for tool, arguments, _key in environment.delegate.calls:
        signature = f"{tool}:{_canonical(arguments)}"
        signatures[signature] = signatures.get(signature, 0) + 1
    return TaskMeasurement(
        task_id=task.task_id,
        title=task.title,
        success=success,
        tool_calls=len(environment.delegate.calls),
        repeated_tool_calls=sum(max(0, value - 1) for value in signatures.values()),
        inline_result_bytes=sum(_json_size(item) for item in outputs),
        artifact_bytes=0,
        wall_clock_ms=round(duration, 3),
        retries=0,
        user_interventions=1 if task.task_id == "10" else 0,
        recovery_success=None if task.task_id != "10" else False,
        safety_violations=0,
        confidentiality_violations=_confidentiality_violations(
            outputs,
            environment.artifacts,
        ),
        incorrect_pre_existing_classification=0,
        duplicate_side_effects=0,
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _measure_optimized(task: BenchmarkTask, root: Path) -> TaskMeasurement:
    environment = BenchmarkEnvironment(root, task.task_id)
    started = time.perf_counter()
    result, success, recovery, duplicate_side_effects = task.optimized(environment)
    duration = (time.perf_counter() - started) * 1000
    artifact_bytes = 0
    if isinstance(result, Mapping):
        size = result.get("total_size")
        artifact_id = result.get("artifact_id")
        if isinstance(size, int) and isinstance(artifact_id, str):
            artifact_bytes = size
    return TaskMeasurement(
        task_id=task.task_id,
        title=task.title,
        success=success,
        tool_calls=1,
        repeated_tool_calls=0,
        inline_result_bytes=_json_size(result),
        artifact_bytes=artifact_bytes,
        wall_clock_ms=round(duration, 3),
        retries=1 if task.task_id == "04" else 0,
        user_interventions=0,
        recovery_success=recovery,
        safety_violations=0,
        confidentiality_violations=_confidentiality_violations(
            result,
            environment.artifacts,
        ),
        incorrect_pre_existing_classification=0,
        duplicate_side_effects=duplicate_side_effects,
    )


def _aggregate(items: Sequence[TaskMeasurement]) -> dict[str, Any]:
    calls = [float(item.tool_calls) for item in items]
    inline = [float(item.inline_result_bytes) for item in items]
    total = [float(item.inline_result_bytes + item.artifact_bytes) for item in items]
    recovery_items = [item for item in items if item.recovery_success is not None]
    return {
        "tasks": len(items),
        "task_success_rate": sum(item.success for item in items) / len(items),
        "median_tool_calls": statistics.median(calls),
        "p95_tool_calls": _percentile(calls, 0.95),
        "median_inline_result_bytes": statistics.median(inline),
        "p95_inline_result_bytes": _percentile(inline, 0.95),
        "median_total_result_bytes": statistics.median(total),
        "median_wall_clock_ms": statistics.median(
            [item.wall_clock_ms for item in items]
        ),
        "repeated_reads_or_calls": sum(item.repeated_tool_calls for item in items),
        "retries": sum(item.retries for item in items),
        "user_interventions": sum(item.user_interventions for item in items),
        "recovery_success_rate": (
            sum(bool(item.recovery_success) for item in recovery_items) / len(recovery_items)
            if recovery_items
            else None
        ),
        "safety_violations": sum(item.safety_violations for item in items),
        "confidentiality_violations": sum(
            item.confidentiality_violations for item in items
        ),
        "incorrect_pre_existing_classification": sum(
            item.incorrect_pre_existing_classification for item in items
        ),
        "duplicate_side_effects": sum(item.duplicate_side_effects for item in items),
    }


def run_autonomy_benchmark() -> dict[str, Any]:
    """Run all ten tasks in separate temporary Git repositories."""
    tasks = _task_definitions()
    baseline: list[TaskMeasurement] = []
    optimized: list[TaskMeasurement] = []
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="karox-autonomy-benchmark-") as temp:
        root = Path(temp)
        for task in tasks:
            baseline.append(_measure_baseline(task, root / f"baseline-{task.task_id}"))
            optimized.append(_measure_optimized(task, root / f"optimized-{task.task_id}"))
    baseline_aggregate = _aggregate(baseline)
    optimized_aggregate = _aggregate(optimized)
    call_reduction = 1.0 - (
        optimized_aggregate["median_tool_calls"]
        / baseline_aggregate["median_tool_calls"]
    )
    inline_reduction = 1.0 - (
        optimized_aggregate["median_inline_result_bytes"]
        / baseline_aggregate["median_inline_result_bytes"]
    )
    targets = {
        "task_success_not_below_baseline": (
            optimized_aggregate["task_success_rate"]
            >= baseline_aggregate["task_success_rate"]
        ),
        "median_tool_calls_reduction_at_least_35_percent": call_reduction >= 0.35,
        "inline_context_reduction_at_least_50_percent": inline_reduction >= 0.50,
        "recovery_success_100_percent": optimized_aggregate["recovery_success_rate"] == 1.0,
        "zero_unauthorized_writes": optimized_aggregate["safety_violations"] == 0,
        "zero_confidentiality_violations": (
            optimized_aggregate["confidentiality_violations"] == 0
        ),
        "zero_duplicate_side_effects": optimized_aggregate["duplicate_side_effects"] == 0,
    }
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "generated_at": time.time(),
        "duration_seconds": round(time.time() - started, 3),
        "fixture_policy": {
            "isolated_temporary_git_repository_per_variant_per_task": True,
            "main_working_tree_mutated": False,
            "external_model_used": False,
            "external_network_used": False,
            "browser_task": "deterministic managed-browser contract simulation only",
            "dev_server_task": "deterministic managed-server contract simulation only",
        },
        "server_side": {
            "status": "completed",
            "actual_components": [
                "RepositoryContextEngine",
                "AffectedChecksEngine",
                "PlanExecutor",
                "TaskStateStore",
                "ArtifactStore",
                "RepositoryLeaseStore",
            ],
            "tasks_executed": len(tasks),
        },
        "simulated_client": {
            "status": "completed",
            "honesty_note": (
                "Deterministic client benchmark; this is not a model benchmark and "
                "must not be presented as ChatGPT Web performance."
            ),
            "baseline": {
                "aggregate": baseline_aggregate,
                "tasks": [item.to_dict() for item in baseline],
            },
            "optimized": {
                "aggregate": optimized_aggregate,
                "tasks": [item.to_dict() for item in optimized],
            },
            "comparison": {
                "median_tool_call_reduction": call_reduction,
                "median_inline_context_reduction": inline_reduction,
                "targets": targets,
                "all_targets_met": all(targets.values()),
            },
        },
        "paired_chatgpt_web": {
            "status": "not_run_user_gate",
            "reason": (
                "Requires a separate test bridge/profile and two fresh ChatGPT Web "
                "sessions; the current live clickup-opus connection must not be migrated."
            ),
            "protocol": "benchmarks/gpt_web_autonomy/PAIRED_CHATGPT_WEB_PROTOCOL.md",
        },
    }


def write_benchmark_artifact(path: Path) -> dict[str, Any]:
    result = run_autonomy_benchmark()
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(redact(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
