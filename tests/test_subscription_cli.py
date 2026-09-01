from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from karox.context_bus import ContextBus
from karox.intelligence_pool import (
    CAP_CODE,
    CAP_REASONING,
    CAP_TOOLS,
    IntelligenceEndpoint,
    IntelligencePool,
    SOURCE_SUBSCRIPTION,
)
from karox.orchestrator import WorkerExecutionRequest
from karox.subscription_cli import (
    TARGET_CLAUDE,
    TARGET_CODEX,
    SubscriptionCliError,
    SubscriptionCliExecutor,
    _ProcessResult,
    _parse_codex,
    discover_subscription_clis,
    register_discovered_subscription_endpoints,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "KaroX Test")
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "base")
    return repo


def _endpoint(target_id: str, *, role: str = "reviewer") -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id="sub:test",
        display_name="Subscription test",
        source_kind=SOURCE_SUBSCRIPTION,
        capabilities=(CAP_CODE, CAP_REASONING, CAP_TOOLS),
        roles=(role,),
        target_id=target_id,
        already_paid=True,
    )


def _request(
    tmp_path: Path,
    endpoint: IntelligenceEndpoint,
    *,
    role: str = "reviewer",
    workspace_path: str | None = None,
    effort_level: str = "medium",
) -> WorkerExecutionRequest:
    bus = ContextBus("run", path=tmp_path / "context.json")
    bus.put_text(
        item_id="task",
        kind="task",
        content="Review app.py. api_key=should-not-leak",
        priority=100,
    )
    return WorkerExecutionRequest(
        run_id="run",
        task_id="task",
        step_id="step",
        idempotency_key="run:step:1",
        role=role,
        task_class="review",
        objective="Review app.py",
        endpoint=endpoint,
        context_delta=bus.delta(role=role, known_hashes={}),
        upstream_messages=(),
        max_cost_usd=1.0,
        effort_level=effort_level,
        workspace_path=workspace_path,
    )


def _process(*, stdout: str = "", exit_code: int = 0) -> _ProcessResult:
    return _ProcessResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        timed_out=False,
        duration_ms=12.0,
        stdout_truncated=False,
        stderr_truncated=False,
    )


def test_discovery_reports_installed_and_manual_adapter_targets(monkeypatch) -> None:
    installed = {
        "codex": "C:/bin/codex.cmd",
        "claude": "C:/bin/claude.cmd",
        "gemini": "C:/bin/gemini.cmd",
        "opencode": "C:/bin/opencode.cmd",
    }

    def which(name: str):
        return installed.get(name.removesuffix(".cmd"))

    monkeypatch.setattr("karox.subscription_cli.shutil.which", which)
    rows = discover_subscription_clis()
    assert {row.profile.target_id for row in rows if row.installed} == {
        "builtin:codex-cli",
        "builtin:claude-code",
        "builtin:gemini-cli",
        "builtin:opencode-cli",
    }
    auto = {row.profile.target_id for row in rows if row.profile.auto_executable}
    assert auto == {TARGET_CODEX, TARGET_CLAUDE}


def test_apply_discovery_registers_only_guarded_builtins_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "karox.subscription_cli.shutil.which",
        lambda name: f"C:/bin/{name}" if name in {"codex", "claude", "gemini", "opencode"} else None,
    )
    pool = IntelligencePool(path=tmp_path / "pool.json")
    endpoints = register_discovered_subscription_endpoints(pool)
    assert {item.target_id for item in endpoints} == {TARGET_CODEX, TARGET_CLAUDE}
    assert all(item.already_paid for item in endpoints)
    assert all(item.source_kind == SOURCE_SUBSCRIPTION for item in endpoints)


def test_codex_read_role_uses_read_only_sandbox_and_safe_prompt(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    endpoint = _endpoint(TARGET_CODEX)
    request = _request(tmp_path, endpoint, effort_level="extra-high")
    calls: list[tuple[list[str], str]] = []

    def fake_run(argv, *, cwd, timeout_seconds, stdin_text=""):
        calls.append((list(argv), stdin_text))
        if argv[:3] == ["git", "status", "--porcelain=v1"]:
            return _process(stdout="")
        if argv[0] == "python":
            return _process(stdout="checks ok")
        assert argv[:2] == ["codex", "exec"]
        return _process(
            stdout="\n".join(
                [
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Review passed"}}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "cached_input_tokens": 60,
                            },
                        }
                    ),
                ]
            )
        )

    monkeypatch.setattr("karox.subscription_cli._run_process", fake_run)
    executor = SubscriptionCliExecutor(
        repo,
        verification_commands=(("python", "-m", "pytest", "-q"),),
    )
    result = executor(request)
    codex_argv, prompt = next((argv, text) for argv, text in calls if argv and argv[0] == "codex")
    assert "--sandbox" in codex_argv
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in codex_argv
    assert "--ignore-rules" in codex_argv
    assert "--strict-config" in codex_argv
    assert 'model_reasoning_effort="xhigh"' in codex_argv
    assert "dangerously" not in " ".join(codex_argv)
    assert "should-not-leak" not in prompt
    assert result.ok and result.verified
    assert result.total_tokens == 120
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 20
    assert result.cache_read_tokens == 60
    assert result.cache_metrics_reported is True


def test_codex_implementer_requires_isolated_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    endpoint = _endpoint(TARGET_CODEX, role="implementer")
    request = _request(tmp_path, endpoint, role="implementer")
    executor = SubscriptionCliExecutor(repo, verification_commands=(("python", "-m", "pytest"),))
    with pytest.raises(SubscriptionCliError, match="isolated KaroX worktree"):
        executor(request)


def test_claude_builtin_is_read_review_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    endpoint = _endpoint(TARGET_CLAUDE, role="implementer")
    request = _request(tmp_path, endpoint, role="implementer", workspace_path=str(repo))
    executor = SubscriptionCliExecutor(repo, verification_commands=(("python", "-m", "pytest"),))
    with pytest.raises(SubscriptionCliError, match="does not permit role implementer"):
        executor(request)


def test_read_role_is_rejected_if_cli_changes_repository(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    endpoint = _endpoint(TARGET_CLAUDE)
    request = _request(tmp_path, endpoint)
    status_calls = 0

    def fake_run(argv, *, cwd, timeout_seconds, stdin_text=""):
        nonlocal status_calls
        if argv[:3] == ["git", "status", "--porcelain=v1"]:
            status_calls += 1
            return _process(stdout="" if status_calls == 1 else " M app.py\x00")
        if argv[0] == "claude":
            return _process(stdout=json.dumps({"result": "Looks good", "usage": {"input_tokens": 10, "output_tokens": 2}}))
        return _process()

    monkeypatch.setattr("karox.subscription_cli._run_process", fake_run)
    executor = SubscriptionCliExecutor(repo, verification_commands=(("python", "-m", "pytest"),))
    result = executor(request)
    assert result.ok is False
    assert result.verified is False
    assert "changed repository state" in result.summary


def test_claude_read_role_uses_safe_mode_and_no_write_tools(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    endpoint = _endpoint(TARGET_CLAUDE)
    request = _request(tmp_path, endpoint, effort_level="ultra")
    seen: list[list[str]] = []

    def fake_run(argv, *, cwd, timeout_seconds, stdin_text=""):
        seen.append(list(argv))
        if argv[:3] == ["git", "status", "--porcelain=v1"]:
            return _process(stdout="")
        if argv[0] == "python":
            return _process(stdout="checks ok")
        return _process(
            stdout=json.dumps(
                {
                    "result": "Independent review complete",
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 5,
                    },
                }
            )
        )

    monkeypatch.setattr("karox.subscription_cli._run_process", fake_run)
    executor = SubscriptionCliExecutor(repo, verification_commands=(("python", "-m", "pytest"),))
    result = executor(request)
    argv = next(item for item in seen if item and item[0] == "claude")
    assert "--safe-mode" in argv
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    tools = argv[argv.index("--tools") + 1]
    assert "Read" in tools
    assert "Edit" not in tools
    assert "Write" not in tools
    assert "Bash" not in tools
    assert "dangerously" not in " ".join(argv)
    assert result.ok and result.verified
    assert result.total_tokens == 50
    assert result.prompt_tokens == 40
    assert result.completion_tokens == 10
    assert result.cache_read_tokens == 30
    assert result.cache_write_tokens == 5
    assert result.cache_metrics_reported is True


def test_codex_cache_is_unavailable_when_cli_does_not_report_it() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                }
            ),
        ]
    )
    _summary, usage = _parse_codex(stdout)
    assert usage.prompt_tokens == 100
    assert usage.cache_read_tokens == 0
    assert usage.cache_metrics_reported is False
