"""Tool-shape fixes from the sandbox SWE traces (2026-11-04).

A pytest run spent three steps on repo.command shapes for one scratch-file
delete and two on lsp.diagnostics with no language server installed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from karox import engine_kernel
from karox.agent import AgentKernel, AgentLimits, ContextBudget, SYSTEM_PROMPT
from karox.core import CoreRuntime
from karox.engine_tool_env import _normalize_repo_command
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.sessions import SessionStore


def test_delete_action_becomes_a_batch_delete() -> None:
    assert _normalize_repo_command({"action": "delete", "payload": {"path": "a.py"}}) == {
        "action": "batch",
        "payload": {"operations": [{"op": "delete", "path": "a.py"}]},
    }
    assert _normalize_repo_command({"action": "remove", "paths": ["a.py", "b.py", "a.py"]}) == {
        "action": "batch",
        "payload": {
            "operations": [
                {"op": "delete", "path": "a.py"},
                {"op": "delete", "path": "b.py"},
            ]
        },
    }


def test_a_json_string_payload_is_decoded() -> None:
    operations = [{"op": "delete", "path": "scratch.py"}]
    assert _normalize_repo_command(
        {"action": "batch", "payload": json.dumps({"operations": operations})}
    ) == {"action": "batch", "payload": {"operations": operations}}
    assert _normalize_repo_command({"action": "batch", "payload": json.dumps(operations)}) == {
        "action": "batch",
        "payload": {"operations": operations},
    }


def test_a_missing_action_is_inferred_only_when_unambiguous() -> None:
    assert _normalize_repo_command({"operations": [{"op": "mkdir", "path": "d"}]}) == {
        "action": "batch",
        "payload": {"operations": [{"op": "mkdir", "path": "d"}]},
    }
    assert _normalize_repo_command({"payload": {"patch": "--- a\n+++ b\n"}}) == {
        "action": "apply_patch",
        "payload": {"patch": "--- a\n+++ b\n"},
    }
    both = {"payload": {"patch": "x", "operations": []}}
    assert _normalize_repo_command(both) == both


def test_unreadable_shapes_pass_through_unchanged() -> None:
    for shape in (
        {},
        {"action": "delete"},
        {"action": "shell", "payload": {"command": "rm -rf /"}},
        {"action": "batch", "payload": "not json"},
        {"action": "batch", "payload": {"operations": []}, "command": "x"},
    ):
        assert _normalize_repo_command(shape) == shape


def _git(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)


def _kernel(tmp_path: Path) -> AgentKernel:
    repository = tmp_path / "repo"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    store = SessionStore(tmp_path / "sessions")
    origin = Origin(OriginKind.NATIVE_AGENT, "shape-test")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    policy.set_grants(
        origin,
        {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.CHECKS_RUN,
            Capability.PROCESS_RUN,
            Capability.GIT_READ,
            Capability.DIAGNOSTICS_READ,
        },
    )
    core = CoreRuntime(
        repository,
        policy,
        store,
        tmp_path / "audit.jsonl",
        verification_commands=[("python", "-c", "print('ok')")],
    )

    class _NoProvider:
        def provider_name(self) -> str:
            return "none"

        def complete(self, request):  # pragma: no cover
            raise AssertionError("not called")

        def close(self) -> None:
            return None

    return AgentKernel(
        provider=_NoProvider(),
        model="test-model",
        core=core,
        sessions=store,
        origin=origin,
        limits=AgentLimits(max_steps=4, max_seconds=30.0),
        system_prompt=SYSTEM_PROMPT,
        context=ContextBudget(),
    )


def _advertised(kernel: AgentKernel) -> set[str]:
    implementation = kernel.implementation
    return {tool.name for tool in implementation._advertised_tools("fix the bug")}


def test_lsp_schema_is_withheld_without_any_language_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_kernel, "_any_language_server", lambda: False)
    hidden = _advertised(_kernel(tmp_path / "a"))
    monkeypatch.setattr(engine_kernel, "_any_language_server", lambda: True)
    shown = _advertised(_kernel(tmp_path / "b"))
    assert not any("lsp" in name for name in hidden)
    assert any("lsp" in name for name in shown)


def test_lsp_timeout_is_clamped_not_refused(tmp_path: Path) -> None:
    from karox.core_types import CoreError

    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "m.py").write_text("x = 1\n", encoding="utf-8")
    runtime = CoreRuntime(
        repository,
        CapabilityPolicy(AccessProfile.WORKSPACE_WRITE),
        SessionStore(tmp_path / "s2"),
        tmp_path / "a2.jsonl",
        verification_commands=[("python", "-c", "print('ok')")],
    )
    # 120 s used to be refused before the server lookup; now it is clamped and
    # the call proceeds to the (possibly missing) language server.
    try:
        runtime._lsp_diagnostics({"path": "m.py", "timeout_seconds": 120}, 60.0)
    except CoreError as exc:
        assert "between 1 and 30" not in str(exc)


def test_visual_checker_usage_reaches_the_session(tmp_path: Path) -> None:
    """The checker's vision calls were missing from session usage and cost."""

    from types import SimpleNamespace

    from karox.providers import ModelResponse

    kernel = _kernel(tmp_path)
    implementation = kernel.implementation
    store = implementation.sessions
    repository = implementation.core.repository
    store.create(repository, "make a page", AccessProfile.WORKSPACE_WRITE, session_id="v1")
    checker = SimpleNamespace(record_usage=None)
    implementation.core._visual_checker = checker
    lease = store.acquire("v1", "test", ttl_seconds=30.0)
    implementation._session_id = "v1"
    implementation._lease = lease
    try:
        # The wiring run() performs before the first step.
        existing = checker.record_usage
        assert existing is None
        checker.record_usage = implementation._record_side_usage
        checker.record_usage(
            ModelResponse(
                content='{"verdict": "pass"}',
                tool_calls=(),
                finish_reason="stop",
                usage={"prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230},
            )
        )
    finally:
        store.release(lease)
    usage = store.load("v1").usage
    assert usage["requests"] == 1
    assert usage["total_tokens"] == 1230
    assert usage["side_requests"] == {"visual_check": 1}
    assert usage["events"][-1]["kind"] == "visual_check"
