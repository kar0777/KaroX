from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.core_tools import ExtendedCoreRuntime
from karox.disk_maintenance import CleanupError
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.risk_mapping import (
    action_for_command,
    command_requests_deletion,
    risk_kind_for,
)
from karox.sessions import SessionStore


def _command(name: str, arguments: dict[str, object]) -> CoreCommand:
    return CoreCommand(
        name=name,
        arguments=arguments,
        session_id="s1",
        origin=Origin(OriginKind.NATIVE_AGENT, "test"),
    )


def test_repo_command_nested_delete_is_high_risk_delete() -> None:
    command = _command(
        "repo.command",
        {
            "action": "batch",
            "payload": {
                "operations": [
                    {"op": "write", "path": "keep.txt", "content": "x"},
                    {"op": "delete", "path": "old.txt"},
                ]
            },
        },
    )
    action = action_for_command(command)
    assert risk_kind_for(command.name, command.arguments) == "repo.delete"
    assert action.kind == "repo.delete"
    assert action.delete_count == 1
    assert set(action.paths) == {"keep.txt", "old.txt"}
    assert command_requests_deletion(command.name, command.arguments) is True


def test_unified_patch_delete_is_detected_before_mutation() -> None:
    arguments = {
        "action": "apply_patch",
        "payload": {
            "patch": "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        },
    }
    assert risk_kind_for("repo.command", arguments) == "repo.delete"
    assert command_requests_deletion("repo.command", arguments) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "cache"],
        ["pwsh", "-Command", "Remove-Item cache -Recurse"],
        ["cmd", "/c", "rmdir /s /q cache"],
        ["git", "clean", "-fd"],
        ["git", "reset", "--hard", "HEAD"],
        ["python", "-c", "import shutil; shutil.rmtree('cache')"],
    ],
)
def test_explicit_process_deletion_is_routed_to_cleanup_review(argv: list[str]) -> None:
    assert command_requests_deletion("dev.command", {"argv": argv}) is True


def test_normal_developer_commands_are_not_mislabeled_as_deletion() -> None:
    assert command_requests_deletion(
        "dev.command", {"argv": ["npm", "install"]}
    ) is False
    assert command_requests_deletion(
        "dev.command", {"argv": ["python", "-m", "pytest"]}
    ) is False


def test_disk_tools_are_metadata_only_and_have_no_model_apply_surface() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repo"
        runtime = Path(temporary) / "runtime"
        initialize_git_repository(root)
        runtime.mkdir()
        (root / "cache").mkdir()
        (root / "cache" / "item.bin").write_bytes(b"x" * 4096)
        sessions = SessionStore(Path(temporary) / "sessions")
        sessions.create(
            root,
            "clean cache",
            AccessProfile.WORKSPACE_WRITE,
            session_id="s1",
        )
        origin = Origin(OriginKind.NATIVE_AGENT, "test")
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(origin, {Capability.DISK_READ})
        with patch("karox.disk_maintenance.runtime_dir", return_value=runtime):
            core = ExtendedCoreRuntime(
                root,
                policy,
                sessions,
                Path(temporary) / "audit.jsonl",
                verification_commands=[],
            )
            tools = {item.name: item for item in core.tools()}
            assert tools["disk.scan"].mutates is False
            assert tools["disk.plan_cleanup"].mutates is False
            assert "disk.apply" not in tools

            scan = core.execute(
                CoreCommand(
                    "disk.scan",
                    {"min_size_mb": 0, "max_candidates": 5, "scan_seconds": 1},
                    "s1",
                    origin,
                )
            )
            assert scan.ok is True
            assert scan.data["metadata_only"] is True
            assert scan.data["content_read"] is False

            plan = core.execute(
                CoreCommand(
                    "disk.plan_cleanup",
                    {"paths": ["cache"]},
                    "s1",
                    origin,
                )
            )
            assert plan.ok is True
            assert plan.mutation is False
            assert plan.data["confirmation_required"] is True
            assert (root / "cache").exists()

            with pytest.raises(PolicyDenied):
                core.execute(CoreCommand("repo.read_file", {"path": "cache/item.bin"}, "s1", origin))


def test_disk_plan_respects_protected_project_root() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "repo"
        runtime = Path(temporary) / "runtime"
        project = root / "project"
        initialize_git_repository(root)
        runtime.mkdir()
        project.mkdir()
        (project / "file.txt").write_text("keep", encoding="utf-8")
        sessions = SessionStore(Path(temporary) / "sessions")
        sessions.create(root, "cleanup", AccessProfile.WORKSPACE_WRITE, session_id="s1")
        origin = Origin(OriginKind.NATIVE_AGENT, "test")
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(origin, {Capability.DISK_READ})
        with patch("karox.disk_maintenance.runtime_dir", return_value=runtime):
            core = ExtendedCoreRuntime(
                root,
                policy,
                sessions,
                Path(temporary) / "audit.jsonl",
                verification_commands=[],
                maintenance_protected_paths=(project,),
            )
            with pytest.raises(CleanupError, match="approved_project_root"):
                core.execute(
                    CoreCommand(
                        "disk.plan_cleanup",
                        {"paths": ["project"]},
                        "s1",
                        origin,
                    )
                )
