from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401
from karox.disk_maintenance import (
    CleanupError,
    apply_cleanup_plan,
    cleanup_impact_preview,
    cleanup_plan_risk,
    create_cleanup_plan,
    load_cleanup_plan,
    scan_cleanup_candidates,
    workspace_system_reason,
)


@pytest.fixture()
def cleanup_workspace() -> tuple[Path, Path]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "drive"
        runtime = Path(temporary) / "runtime"
        root.mkdir()
        runtime.mkdir()
        with patch("karox.disk_maintenance.runtime_dir", return_value=runtime):
            yield root, runtime


def _write(path: Path, size: int = 256) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_scan_is_metadata_only_and_classifies_rebuildable_project_cache(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    project = root / "site"
    (project / ".git").mkdir(parents=True)
    _write(project / "node_modules" / "pkg" / "index.js", 2048)

    result = scan_cleanup_candidates(root, max_candidates=10, min_size_mb=0, scan_seconds=2)

    assert result["metadata_only"] is True
    assert result["content_read"] is False
    row = next(item for item in result["candidates"] if item["path"] == "site/node_modules")
    assert row["category"] == "project_dependencies"
    assert row["data_risk"] == "rebuildable"
    assert row["code_project"] == "site"
    assert "Node.js" in row["affected_apps"]
    assert row["size_bytes"] >= 2048


def test_system_subdirectory_is_rejected_even_when_parent_drive_is_allowed(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    windows = root / "Windows"
    _write(windows / "Temp" / "junk.tmp")

    with patch.dict(os.environ, {"SystemRoot": str(windows), "windir": str(windows)}, clear=False):
        assert workspace_system_reason(windows) == "windows_system_folder"
        with pytest.raises(CleanupError, match="protected"):
            create_cleanup_plan(root, ["Windows/Temp"])


def test_project_root_can_be_protected_while_rebuildable_child_can_be_cleaned(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    project = root / "app"
    (project / ".git").mkdir(parents=True)
    _write(project / "package.json", 128)
    _write(project / "node_modules" / "left-pad" / "index.js", 4096)

    with pytest.raises(CleanupError, match="approved_project_root"):
        create_cleanup_plan(root, ["app"], protected_roots=(project,))

    scan = scan_cleanup_candidates(
        root,
        max_candidates=10,
        min_size_mb=0,
        scan_seconds=2,
        protected_roots=(project,),
    )
    assert any(item["path"] == "app/node_modules" for item in scan["candidates"])
    assert all(item["path"] != "app" for item in scan["candidates"])

    public = create_cleanup_plan(
        root,
        ["app/node_modules"],
        protected_roots=(project,),
    )
    assert public["confirmation_required"] is True
    assert public["irreversible"] is True
    assert public["code_projects"] == ("app",) or public["code_projects"] == ["app"]
    assert public["targets"][0]["category"] == "project_dependencies"
    assert "fingerprint" not in public["targets"][0]


def test_apply_requires_explicit_confirmation(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    _write(root / "cache" / "a.bin", 1024)
    plan = create_cleanup_plan(root, ["cache"])

    with pytest.raises(CleanupError, match="explicit user confirmation"):
        apply_cleanup_plan(plan["plan_id"], root=root, confirmed_by_user=False)
    assert (root / "cache" / "a.bin").exists()
    assert load_cleanup_plan(plan["plan_id"], public=True)["plan_id"] == plan["plan_id"]


def test_plan_is_invalidated_when_target_changes_after_preview(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    _write(root / "cache" / "a.bin", 1024)
    plan = create_cleanup_plan(root, ["cache"])
    _write(root / "cache" / "new.bin", 333)

    with pytest.raises(CleanupError, match="changed after preview"):
        apply_cleanup_plan(plan["plan_id"], root=root, confirmed_by_user=True)
    assert (root / "cache" / "a.bin").exists()
    assert (root / "cache" / "new.bin").exists()


def test_confirmed_plan_deletes_frozen_target_and_writes_audit(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, runtime = cleanup_workspace
    _write(root / "cache" / "a.bin", 4096)
    _write(root / "cache" / "nested" / "b.bin", 2048)
    plan = create_cleanup_plan(root, ["cache"])

    result = apply_cleanup_plan(plan["plan_id"], root=root, confirmed_by_user=True)

    assert result["status"] == "completed"
    assert result["removed_target_count"] == 1
    assert result["planned_bytes"] >= 6144
    assert result["deleted_paths"] == ["cache"]
    assert not (root / "cache").exists()
    assert (runtime / "cleanup_audit.jsonl").is_file()
    with pytest.raises(CleanupError, match="does not exist|already completed"):
        load_cleanup_plan(plan["plan_id"])


def test_cleanup_plan_refuses_workspace_root_and_git_metadata(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    (root / ".git").mkdir()
    _write(root / ".git" / "index", 64)
    _write(root / "ordinary.txt", 64)

    with pytest.raises(CleanupError):
        create_cleanup_plan(root, ["."])
    with pytest.raises(CleanupError, match="git_metadata"):
        create_cleanup_plan(root, [".git"])


def test_cleanup_preview_is_compact_and_uses_whole_plan_risk(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    project = root / "site"
    (project / ".git").mkdir(parents=True)
    _write(project / "node_modules" / "pkg" / "index.js", 4096)
    plan = create_cleanup_plan(root, ["site/node_modules"])

    assert plan["target_count"] == 1
    assert plan["risk_level"] == "rebuildable"
    assert cleanup_plan_risk(plan) == "rebuildable"
    preview = cleanup_impact_preview(plan, language="ru")
    assert len(preview.splitlines()) <= 4
    assert "Удалится:" in preview
    assert "Node.js" in preview
    assert "site" in preview
    assert "восстанавливаемые" in preview
    assert "Необратимо" in preview


def test_cleanup_preview_warns_about_personal_or_code_data() -> None:
    plan = {
        "total_bytes": 1024,
        "file_count": 1,
        "target_count": 9,
        # The public list may be truncated, but the authoritative aggregate risk
        # must still win over the visible target subset.
        "targets": [{"data_risk": "rebuildable"}],
        "risk_level": "personal_or_code",
        "affected_apps": [],
        "code_projects": ["important-project"],
    }
    assert cleanup_plan_risk(plan) == "personal_or_code"
    preview = cleanup_impact_preview(plan, language="en")
    assert "9 targets" in preview
    assert "code or personal data" in preview


def test_parent_of_approved_project_cannot_be_deleted_but_scan_can_descend(
    cleanup_workspace: tuple[Path, Path],
) -> None:
    root, _runtime = cleanup_workspace
    parent = root / "work"
    project = parent / "app"
    (project / ".git").mkdir(parents=True)
    _write(project / "node_modules" / "pkg" / "index.js", 4096)

    with pytest.raises(CleanupError, match="approved_project_parent"):
        create_cleanup_plan(root, ["work"], protected_roots=(project,))

    scan = scan_cleanup_candidates(
        root,
        max_candidates=10,
        min_size_mb=0,
        scan_seconds=2,
        protected_roots=(project,),
    )
    assert any(item["path"] == "work/app/node_modules" for item in scan["candidates"])
