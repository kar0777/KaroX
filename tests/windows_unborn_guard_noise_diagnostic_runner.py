from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.artifacts import ArtifactStore
from karox.repo_context import RepositoryContextEngine
from karox.workspace_change_guard import start_workspace_change_guard

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_windows_unborn_guard_noise.json"


def _snapshot(guard) -> dict[str, object]:
    return {
        "changed": guard.changed,
        "failed": guard.failed,
        "changed_paths": list(guard.changed_paths),
        "ignored_git_events": guard.ignored_git_events,
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows-only watcher diagnostic")
def test_unborn_guard_noise() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo = root / "repo"
        initialize_git_repository(repo)
        (repo / "src").mkdir()
        (repo / "src" / "value.txt").write_bytes(b"before\n")
        old_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        try:
            context = RepositoryContextEngine(
                repo,
                ArtifactStore("unborn-guard-diagnostic"),
                policy_profile="workspace_write",
            )
            stages: dict[str, object] = {}
            guard = start_workspace_change_guard(repo)
            assert guard.supported
            stages["started"] = _snapshot(guard)
            context._fast_revision_identity()
            time.sleep(0.03)
            stages["after_fast_identity"] = _snapshot(guard)
            context._git_control_identity()
            time.sleep(0.03)
            stages["after_git_control"] = _snapshot(guard)
            (repo / "src" / "value.txt").read_bytes()
            time.sleep(0.03)
            stages["after_read"] = _snapshot(guard)
            guard.finish()
            stages["after_finish"] = _snapshot(guard)
        finally:
            if old_runtime is None:
                os.environ.pop("KAROX_RUNTIME_DIR", None)
            else:
                os.environ["KAROX_RUNTIME_DIR"] = old_runtime
    payload = {
        "schema_version": 1,
        "benchmark": "windows-unborn-repository-change-guard-noise",
        "stages": stages,
        "isolation": {"temporary_repository": True, "live_bridge_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
