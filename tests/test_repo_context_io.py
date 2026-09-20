"""Strict content identities should not repeat per-entry filesystem queries."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC  # noqa: F401
from karox.repo_context import RepositoryContextEngine


class RepositoryContextIOTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.engine = object.__new__(RepositoryContextEngine)
        self.engine.repository = self.root
        self.engine.cache_root = self.root / "cache"
        self.engine.cache_root.mkdir()
        self.engine.artifacts = mock.Mock()

    def test_content_tree_uses_one_metadata_query_per_entry(self) -> None:
        tree = self.root / "untracked"
        branch = tree / "nested"
        branch.mkdir(parents=True)
        for index in range(12):
            (branch / f"module_{index:02}.py").write_bytes(f"VALUE = {index}\n".encode())
        ignored = tree / "__pycache__"
        ignored.mkdir()
        (ignored / "ignored.pyc").write_bytes(b"ignored")
        expected = hashlib.sha256()
        markers = ["??\0untracked\0dir", "??\0untracked/nested\0dir"]
        for path in sorted(branch.iterdir()):
            content = path.read_bytes()
            relative = path.relative_to(self.root).as_posix()
            markers.append(
                f"??\0{relative}\0file\0{len(content)}\0{hashlib.sha256(content).hexdigest()}"
            )
        for marker in markers:
            expected.update(marker.encode() + b"\0")
        calls = []
        original = Path.stat

        def counted(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", counted):
            actual = self.engine._content_tree_digest(tree, status_code="??", relative="untracked")
        self.assertEqual(actual, expected.hexdigest())
        self.assertEqual(len(calls), 14, "one lstat for each of 2 directories and 12 files")

    def test_content_tree_detects_same_size_same_mtime_rewrite(self) -> None:
        path = self.root / "module.py"
        path.write_bytes(b"VALUE = 1\n")
        before = path.stat()
        first = self.engine._content_tree_digest(path, status_code=" M", relative="module.py")
        path.write_bytes(b"VALUE = 2\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = self.engine._content_tree_digest(path, status_code=" M", relative="module.py")
        self.assertNotEqual(first, second)

    def test_content_tree_hashes_symlink_target_without_opening_it(self) -> None:
        path = self.root / "link"
        metadata = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 8, 0, 0, 0))
        expected = hashlib.sha256(b"??\0link\0symlink\0../other\0").hexdigest()
        with (
            mock.patch.object(Path, "stat", return_value=metadata),
            mock.patch("os.readlink", return_value="../other"),
            mock.patch.object(Path, "open", side_effect=AssertionError("must not follow symlinks")),
        ):
            actual = self.engine._content_tree_digest(path, status_code="??", relative="link")
        self.assertEqual(actual, expected)

    def test_literal_arrow_in_git_path_still_invalidates_identity(self) -> None:
        # Windows forbids '>' in real filenames. Exercise the same porcelain
        # record with in-memory file reads instead of skipping this regression.
        self.engine.is_git_repository = True
        name = "literal -> name.py"
        content = {name: b"VALUE = 1\n"}
        metadata = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 10, 0, 0, 0))

        def read(path):
            return content.get(path.name, b"unrelated")

        with (
            mock.patch.object(self.engine, "_status_snapshot", return_value=("rev", f" M {name}\0")),
            mock.patch("karox.repo_context._safe_relative", side_effect=lambda _root, value: value),
            mock.patch.object(Path, "stat", return_value=metadata),
            mock.patch.object(Path, "read_bytes", read),
            mock.patch.object(Path, "open", lambda path, *_args, **_kwargs: io.BytesIO(read(path))),
        ):
            before = self.engine._compact_content_revision_identity()
            strict_before = self.engine._revision_identity()
            content[name] = b"VALUE = 2\n"
            after = self.engine._compact_content_revision_identity()
            strict_after = self.engine._revision_identity()
        self.assertNotEqual(before, after)
        self.assertNotEqual(strict_before, strict_after)
        self.assertEqual(after["dirty"][0]["path"], name)

    def test_rename_source_is_not_treated_as_a_second_status_record(self) -> None:
        self.engine.is_git_repository = True
        (self.root / "new.py").write_bytes(b"VALUE = 1\n")
        # A source-path record has no status prefix. Stripping three bytes
        # from it accidentally points at this unrelated real file.
        (self.root / "src").mkdir()
        (self.root / "src" / "extra.py").write_bytes(b"UNRELATED = 1\n")
        for code in ("R ", " R", "C "):
            with self.subTest(code=code), mock.patch.object(
                self.engine, "_status_snapshot",
                return_value=("rev", f"{code} new.py\0oldsrc/extra.py\0"),
            ):
                for method in (
                    self.engine._revision_identity,
                    self.engine._compact_content_revision_identity,
                    self.engine._fast_revision_identity,
                ):
                    self.assertEqual(
                        [item["path"] for item in method()["dirty"]],
                        ["new.py", "oldsrc/extra.py"],
                    )

    def test_changing_rename_source_still_invalidates_identity(self) -> None:
        self.engine.is_git_repository = True
        (self.root / "new.py").write_bytes(b"VALUE = 1\n")
        for method in (
            self.engine._revision_identity,
            self.engine._compact_content_revision_identity,
            self.engine._fast_revision_identity,
        ):
            with mock.patch.object(
                self.engine, "_status_snapshot", return_value=("rev", "R  new.py\0a.py\0"),
            ):
                first = method()
            with mock.patch.object(
                self.engine, "_status_snapshot", return_value=("rev", "R  new.py\0b.py\0"),
            ):
                second = method()
            self.assertNotEqual(first, second)

    def test_non_object_cache_json_is_a_miss(self) -> None:
        for value in ([], None, "not an object", 42, True):
            with self.subTest(value=value):
                (self.engine.cache_root / "key.json").write_text(json.dumps(value), encoding="utf-8")
                self.assertIsNone(self.engine._cache_get("key"))
        self.engine.artifacts.exists.assert_not_called()


if __name__ == "__main__":
    unittest.main()
