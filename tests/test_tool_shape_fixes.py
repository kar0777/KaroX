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


# -- a pure-deletion patch of the session's own scratch file ---------------

from karox.engine_tool_env import _TOOL_ALTERNATIVE, scratch_delete_paths  # noqa: E402


def test_a_deletion_patch_of_an_own_file_needs_no_exact_body() -> None:
    """The 3D run's first try: a guessed body for its own probe.html."""

    patch = "--- a/probe.html\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-<![doctype]>\n"
    assert scratch_delete_paths({"patch": patch}, ["probe.html"]) == ["probe.html"]
    assert scratch_delete_paths({"payload": {"patch": patch}}, ["probe.html"]) == ["probe.html"]
    two = (
        "diff --git a/p1.py b/p1.py\ndeleted file mode 100644\n"
        "--- a/p1.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x = 1\n--- not a header\n"
        "--- a/sub\\p2.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-y\n"
    )
    assert scratch_delete_paths({"patch": two}, ["p1.py", "sub/p2.py"]) == ["p1.py", "sub/p2.py"]


def test_other_patches_still_go_to_core_as_sent() -> None:
    delete = "--- a/probe.html\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    edit = "--- a/app.js\n+++ b/app.js\n@@ -1 +1 @@\n-a\n+b\n"
    create = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
    # Not created by the session: the user's file keeps Core's exact check.
    assert scratch_delete_paths({"patch": delete}, ["other.html"]) == []
    assert scratch_delete_paths({"patch": delete}, []) == []
    # Mixed with an edit or a creation: not a pure delete.
    assert scratch_delete_paths({"patch": delete + edit}, ["probe.html", "app.js"]) == []
    assert scratch_delete_paths({"patch": create}, ["new.py"]) == []
    assert scratch_delete_paths({"patch": "--- a/probe.html\n+++ /dev/null\n+x\n"}, ["probe.html"]) == []
    for shape in ({}, {"patch": ""}, {"patch": 3}, {"payload": "x"}):
        assert scratch_delete_paths(shape, ["probe.html"]) == []


def test_a_disabled_patch_tool_names_the_delete_operation() -> None:
    text = _TOOL_ALTERNATIVE["repo.apply_patch"]
    assert '"op": "delete"' in text and "repo_command" in text


def test_a_deletion_patch_of_files_already_gone_is_recognized(tmp_path) -> None:
    from karox.engine_tool_env import _requested_dry_run, absent_delete_paths

    (tmp_path / "kept.py").write_text("x\n", encoding="utf-8")
    gone = "--- a/probe.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    kept = "--- a/kept.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    outside = "--- a/../escape.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    edit = "--- a/probe.py\n+++ b/probe.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert absent_delete_paths({"patch": gone}, tmp_path) == ["probe.py"]
    assert absent_delete_paths({"patch": gone + kept}, tmp_path) == []
    assert absent_delete_paths({"patch": outside}, tmp_path) == []
    assert absent_delete_paths({"patch": edit}, tmp_path) == []
    assert absent_delete_paths({"patch": gone}, None) == []
    assert _requested_dry_run({"dry_run": True}) is True
    assert _requested_dry_run({"payload": {"dry_run": True}}) is True
    assert _requested_dry_run({"dry_run": "yes"}) is False


def test_top_level_old_and_new_mean_old_string_and_new_string() -> None:
    from karox.engine_tool_env import _normalize_edit_file

    assert _normalize_edit_file({"path": "a.py", "old": "x", "new": "y"}) == {
        "path": "a.py",
        "old_string": "x",
        "new_string": "y",
    }
    assert _normalize_edit_file({"path": "a.py", "old": "x", "new_string": "y"}) == {
        "path": "a.py",
        "old_string": "x",
        "new_string": "y",
    }
    # The canonical spelling wins and the edits form is left alone.
    both = {"path": "a.py", "old": "x", "old_string": "z", "new_string": "y"}
    assert _normalize_edit_file(both) == both
    batch = {"path": "a.py", "edits": [{"old": "x", "new": "y"}], "old": "q"}
    assert _normalize_edit_file(batch) == batch
