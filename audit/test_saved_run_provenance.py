"""Offline provenance regression tests. Never run an LLM or publish a leaderboard."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from verify_saved_run_provenance import EvidenceIntegrityError, verify  # noqa: E402

HISTORICAL = ROOT / "paired-linux/linux-real-rich-epoch1/real-rich-4246"
TASK = ROOT / "tasks/real-rich-4246"


@pytest.mark.parametrize("arm", ["karox5", "opencode_v2", "deepseek_harness"])
def test_real_historical_rich_patch_and_workspace_match_agent_ledger(arm: str) -> None:
    result = verify(HISTORICAL / arm, TASK)
    assert result["valid"] is True
    assert result["arm"] == arm
    assert result["patch_bytes"] == 432
    assert result["baseline_files"] > 80


def _copy_arm(tmp_path: Path) -> Path:
    folder = tmp_path / "karox5"
    folder.mkdir()
    source = HISTORICAL / "karox5"
    shutil.copy2(source / "result.json", folder / "result.json")
    shutil.copy2(source / "changes.patch", folder / "changes.patch")
    shutil.copytree(source / "workspace", folder / "workspace")
    return folder


def test_patch_modified_after_agent_run_is_rejected(tmp_path: Path) -> None:
    folder = _copy_arm(tmp_path)
    patch = folder / "changes.patch"
    patch.write_bytes(patch.read_bytes() + b"\n# forged patch suffix\n")
    with pytest.raises(EvidenceIntegrityError, match="patch"):
        verify(folder, TASK)


def test_worktree_modified_after_agent_run_is_rejected(tmp_path: Path) -> None:
    folder = _copy_arm(tmp_path)
    file = folder / "workspace" / "TASK.md"
    file.write_bytes(file.read_bytes() + b"\nforged workspace edit\n")
    with pytest.raises(EvidenceIntegrityError, match="Saved workspace differs"):
        verify(folder, TASK)


def test_raw_result_changed_to_different_task_is_rejected(tmp_path: Path) -> None:
    import json

    folder = _copy_arm(tmp_path)
    file = folder / "result.json"
    raw = json.loads(file.read_text(encoding="utf-8"))
    raw["task_id"] = "another-task"
    file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(EvidenceIntegrityError, match="task id"):
        verify(folder, TASK)
