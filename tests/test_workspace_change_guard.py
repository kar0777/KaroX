from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401
from karox.workspace_change_guard import WorkspaceChangeGuard, start_workspace_change_guard


class WorkspaceChangeGuardFilterTests(unittest.TestCase):
    def test_generated_runtime_churn_is_ignored(self) -> None:
        for path in (
            r"node_modules\.vite-temp\config.mjs",
            r".netlify\functions-serve\generated.js",
            r"dist\bundle.js",
            r"coverage\index.html",
        ):
            with self.subTest(path=path):
                self.assertTrue(WorkspaceChangeGuard._is_ignored_generated_path(path))
        self.assertFalse(WorkspaceChangeGuard._is_ignored_generated_path(r"src\app.ts"))
        self.assertFalse(WorkspaceChangeGuard._is_ignored_generated_path(r".git\index.lock"))


@unittest.skipUnless(os.name == "nt", "Windows-only native change guard")
class WorkspaceChangeGuardTests(unittest.TestCase):
    def test_quiet_directory_finishes_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guard = start_workspace_change_guard(root)
            self.assertTrue(guard.supported)
            self.assertTrue(guard.finish())

    def test_same_size_write_with_restored_mtime_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "same-size.txt"
            target.write_bytes(b"alpha\n")
            before = target.stat()
            guard = start_workspace_change_guard(root)
            self.assertTrue(guard.supported)
            time.sleep(0.02)
            target.write_bytes(b"omega\n")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertFalse(guard.finish())
            self.assertTrue(guard.changed)

    def test_git_control_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git_dir = root / ".git"
            git_dir.mkdir()
            guard = start_workspace_change_guard(root)
            self.assertTrue(guard.supported)
            time.sleep(0.02)
            (git_dir / "index.lock").write_bytes(b"change")
            self.assertFalse(guard.finish())
            self.assertTrue(guard.changed)
            self.assertTrue(any(".git" in path.lower() for path in guard.changed_paths))

    def test_external_gitdir_marker_uses_conservative_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").write_text("gitdir: ../external-gitdir\n", encoding="utf-8")
            guard = start_workspace_change_guard(root)
            self.assertFalse(guard.supported)
            self.assertFalse(guard.finish())

    def test_submodule_repository_uses_conservative_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitmodules").write_text("[submodule \"x\"]\n", encoding="utf-8")
            guard = start_workspace_change_guard(root)
            self.assertFalse(guard.supported)
            self.assertFalse(guard.finish())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
