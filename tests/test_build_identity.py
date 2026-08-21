"""Version/build identity: "old installed code" must be tellable at a glance.

The stale-slash-menu report is diagnosable only if the product states which
code answered: version, layout (source checkout vs installed package), commit,
and where the package was imported from. Unknown facts stay "unknown".
"""

from __future__ import annotations

import unittest
from pathlib import Path

from _support import SRC  # noqa: F401

from karox.build_identity import (
    BuildIdentity,
    _detect_commit,
    _detect_layout,
    build_identity,
)


class LayoutDetectionTests(unittest.TestCase):
    def test_site_packages_is_installed(self) -> None:
        path = Path("/opt/py/lib/python3.13/site-packages/karox")
        self.assertEqual(_detect_layout(path), "installed-package")

    def test_src_checkout_with_pyproject(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src" / "karox"
            package.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            self.assertEqual(_detect_layout(package), "source-checkout")

    def test_anything_else_is_unknown(self) -> None:
        self.assertEqual(_detect_layout(Path("/somewhere/else/karox")), "unknown")


class CommitDetectionTests(unittest.TestCase):
    def _fake_checkout(self, tmp: str) -> Path:
        root = Path(tmp)
        package = root / "src" / "karox"
        package.mkdir(parents=True)
        (root / ".git").mkdir()
        return package

    def test_branch_ref_resolves_to_short_commit(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            package = self._fake_checkout(tmp)
            git = Path(tmp) / ".git"
            (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git / "refs" / "heads").mkdir(parents=True)
            (git / "refs" / "heads" / "main").write_text(
                "0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8"
            )
            self.assertEqual(_detect_commit(package), "0123456789ab")

    def test_packed_refs_fallback(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            package = self._fake_checkout(tmp)
            git = Path(tmp) / ".git"
            (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git / "packed-refs").write_text(
                "# pack-refs with: peeled fully-peeled sorted\n"
                "fedcba9876543210fedcba9876543210fedcba98 refs/heads/main\n",
                encoding="utf-8",
            )
            self.assertEqual(_detect_commit(package), "fedcba987654")

    def test_missing_git_is_unknown_not_guessed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "src" / "karox"
            package.mkdir(parents=True)
            self.assertEqual(_detect_commit(package), "unknown")


class RunningIdentityTests(unittest.TestCase):
    def test_identity_never_raises_and_names_the_running_package(self) -> None:
        identity = build_identity()
        self.assertIsInstance(identity, BuildIdentity)
        self.assertTrue(identity.package_path.endswith("karox"))
        self.assertIn(
            identity.layout, {"source-checkout", "installed-package", "unknown"}
        )
        # The test process imports from the source checkout.
        self.assertEqual(identity.layout, "source-checkout")
        self.assertNotEqual(identity.source_mtime, "")

    def test_summary_is_one_line_of_facts(self) -> None:
        identity = build_identity()
        summary = identity.summary()
        self.assertIn(identity.version, summary)
        self.assertIn(identity.layout, summary)
        self.assertNotIn("\n", summary)


if __name__ == "__main__":
    unittest.main()
