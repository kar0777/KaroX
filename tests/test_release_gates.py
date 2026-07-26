"""The scripts that stand between a commit and a published release.

`check_versions.py` and `check_test_count.py` are wired into CI as gates, and
`release.yml` will not publish unless they pass. A gate that refuses a legitimate
state is as damaging as one that waves a broken state through -- more so here,
because the state it refused was the version bump that the only writer of the
file it complains about depends on.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from _support import ROOT  # noqa: F401 - inserts src on sys.path


def _run_quietly(gate: ModuleType, argv: list[str]) -> int:
    """Run a checker without its report landing in the suite's own output.

    These scripts are built to talk to a human on stdout, which is right for CI
    and noise inside a test run.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        result = gate.main(argv)
    return int(result)


def _load_gate(name: str) -> ModuleType:
    """Import a checker from scripts/, which is not an importable package."""
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_gate_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MINIMAL_PYPROJECT = textwrap.dedent(
    """\
    [project]
    name = "karox"
    dynamic = ["version"]

    [tool.setuptools.dynamic]
    version = { attr = "karox.__version__" }
    """
)


class CheckVersionsTests(unittest.TestCase):
    """A release marker may lag the version it records; it may never lead it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.gate = _load_gate("check_versions")

        (self.root / "src" / "karox").mkdir(parents=True)
        (self.root / "src" / "karox" / "__init__.py").write_text(
            '__version__ = "5.0.0.dev0"\n', encoding="utf-8"
        )
        (self.root / "pyproject.toml").write_text(_MINIMAL_PYPROJECT, encoding="utf-8")
        (self.root / "server").mkdir()
        (self.root / ".github" / "workflows").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_release(self, *, version: str, recorded: str) -> None:
        """Lay out the tree a developer produces when bumping to ``version``."""
        (self.root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        (self.root / "server" / "repo_tools.py").write_text(
            f'VERSION = "{version}"\n', encoding="utf-8"
        )
        (self.root / f"RELEASE_NOTES_v{version}.md").write_text("notes\n", encoding="utf-8")
        (self.root / "RELEASE.json").write_text(
            json.dumps({"status": "published", "version": recorded}), encoding="utf-8"
        )

    def _run(self) -> int:
        with patch.object(self.gate, "ROOT", self.root):
            return _run_quietly(self.gate, [])

    def test_a_marker_that_lags_the_bump_is_accepted(self) -> None:
        """This is the state of every version bump, and it used to fail.

        RELEASE.json is written only by the publish job, which runs after this
        gate, so demanding equality made the gate refuse the one commit that
        could ever produce the state it was demanding. Nothing could be released.
        """
        self._write_release(version="4.1.5", recorded="4.1.4")
        self.assertEqual(self._run(), 0)

    def test_a_marker_ahead_of_the_version_is_refused(self) -> None:
        self._write_release(version="4.1.5", recorded="4.1.6")
        self.assertEqual(self._run(), 1)

    def test_a_marker_recording_the_current_version_is_accepted(self) -> None:
        self._write_release(version="4.1.5", recorded="4.1.5")
        self.assertEqual(self._run(), 0)

    def test_ordering_is_numeric_not_lexicographic(self) -> None:
        """`4.1.10` follows `4.1.9`; compared as text it would precede it."""
        self._write_release(version="4.1.10", recorded="4.1.9")
        self.assertEqual(self._run(), 0)
        self._write_release(version="4.1.9", recorded="4.1.10")
        self.assertEqual(self._run(), 1)

    def test_a_malformed_marker_version_does_not_crash_the_gate(self) -> None:
        self._write_release(version="4.1.5", recorded="not-a-version")
        # Sorts as 0.0.0, so it lags rather than leads: reported by other checks
        # if it matters, but never as a traceback out of a release gate.
        self.assertEqual(self._run(), 0)


class CheckTestCountTests(unittest.TestCase):
    """The published suite size is evidence, so a stale copy has to fail."""

    def setUp(self) -> None:
        self.gate = _load_gate("check_test_count")

    def test_the_counts_this_repository_publishes_are_current(self) -> None:
        """The real check, against the real tree -- all three copies read 536
        against a suite of 576 before this gate existed."""
        self.assertEqual(_run_quietly(self.gate, []), 0)

    def test_a_stale_published_count_fails(self) -> None:
        self.assertEqual(_run_quietly(self.gate, ["--print", "suite"]), 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A document that states a number one short of the truth.
            (root / "docs" / "vNext").mkdir(parents=True)
            (root / "README.md").write_text(
                "The suite is 1 tests.\n`Ran 1 tests`\n1 is the number\nroot collects 1\n",
                encoding="utf-8",
            )
            with (
                patch.object(self.gate, "ROOT", root),
                patch.object(
                    self.gate, "SUITE_COUNT_CLAIMS", (("README.md", r"The suite is (\d+) tests"),)
                ),
                patch.object(self.gate, "ROOT_COUNT_CLAIMS", ()),
                patch.object(self.gate, "LEGACY_CHECKS", root / "absent.py"),
                patch.object(self.gate, "_discover", lambda *a, **k: [object()] * 7),
            ):
                self.assertEqual(_run_quietly(self.gate, []), 1)

    def test_a_test_module_that_stops_importing_is_reported(self) -> None:
        """unittest turns an unimportable module into one synthetic failing test,
        so the total would merely look smaller rather than wrong."""

        class _FailedTest:
            def id(self) -> str:
                return "unittest.loader._FailedTest.test_broken"

        broken = _FailedTest()
        self.assertEqual(
            self.gate._import_failures([broken]),  # type: ignore[list-item]
            ["unittest.loader._FailedTest.test_broken"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
