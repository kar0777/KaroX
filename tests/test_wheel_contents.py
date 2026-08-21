"""The wheel a user installs must be the code in the repository.

These tests build synthetic wheels rather than invoking the real build, so they
stay fast and deterministic while still pinning the two failure modes the gate
exists for: a module that survives in ``build/lib`` after being deleted from
source, and missing Chrome-extension package data.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

import importlib.util

_GATE = Path(__file__).resolve().parents[1] / "scripts" / "check_wheel_contents.py"
_SPEC = importlib.util.spec_from_file_location("check_wheel_contents", _GATE)
assert _SPEC and _SPEC.loader
check_wheel_contents = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_wheel_contents)


PACKAGE_DATA = (
    "karox/browser_extension/manifest.json",
    "karox/browser_extension/service_worker.js",
)


class WheelContentsGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "src" / "karox"
        (self.package / "browser_extension").mkdir(parents=True)
        for name in ("__init__.py", "core.py", "risk_engine.py"):
            (self.package / name).write_text("", encoding="utf-8")
        (self.package / "browser_extension" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (self.root / "dist").mkdir()

    def build_wheel(self, names: tuple[str, ...], filename: str = "pkg.whl") -> Path:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in names:
                archive.writestr(name, "")
        path = self.root / "dist" / filename
        path.write_bytes(buffer.getvalue())
        return path

    def complete_names(self) -> tuple[str, ...]:
        return (
            "karox/__init__.py",
            "karox/core.py",
            "karox/risk_engine.py",
            "karox/browser_extension/__init__.py",
            *PACKAGE_DATA,
        )

    def test_a_matching_wheel_passes(self) -> None:
        wheel = self.build_wheel(self.complete_names())
        self.assertEqual(
            check_wheel_contents.collect_problems(self.root, wheel), []
        )

    def test_a_stale_module_from_an_uncleaned_build_directory_is_caught(self) -> None:
        # The real defect: setuptools zips build/lib, which it never prunes, so
        # a deleted module keeps shipping forever.
        wheel = self.build_wheel(
            (*self.complete_names(), "karox/markdown_render.py")
        )
        problems = check_wheel_contents.collect_problems(self.root, wheel)
        self.assertEqual(len(problems), 1)
        self.assertIn("markdown_render.py", problems[0])
        self.assertIn("delete build/", problems[0])

    def test_a_module_missing_from_the_wheel_is_caught(self) -> None:
        names = tuple(
            name for name in self.complete_names() if name != "karox/risk_engine.py"
        )
        problems = check_wheel_contents.collect_problems(
            self.root, self.build_wheel(names)
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("missing karox/risk_engine.py", problems[0])

    def test_missing_extension_package_data_is_caught(self) -> None:
        names = tuple(
            name for name in self.complete_names() if name not in PACKAGE_DATA
        )
        problems = check_wheel_contents.collect_problems(
            self.root, self.build_wheel(names)
        )
        self.assertEqual(len(problems), len(PACKAGE_DATA))
        self.assertTrue(all("package data" in item for item in problems))

    def test_nested_package_modules_are_compared_by_full_path(self) -> None:
        # A nested module must not be matched by basename alone, or a file moved
        # between packages would look unchanged.
        (self.package / "browser_extension" / "helper.py").write_text(
            "", encoding="utf-8"
        )
        wheel = self.build_wheel((*self.complete_names(), "karox/helper.py"))
        problems = check_wheel_contents.collect_problems(self.root, wheel)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("ships karox/helper.py" in item for item in problems))
        self.assertTrue(
            any("missing karox/browser_extension/helper.py" in item for item in problems)
        )

    def test_the_newest_wheel_is_selected(self) -> None:
        import os
        import time

        older = self.build_wheel(self.complete_names(), filename="old.whl")
        time.sleep(0.01)
        newer = self.build_wheel(self.complete_names(), filename="new.whl")
        os.utime(older, (1, 1))
        self.assertEqual(
            check_wheel_contents.newest_wheel(self.root / "dist"), newer
        )

    @staticmethod
    def _run_gate(argv: list[str]) -> tuple[int, str]:
        """Run the gate with its human report captured, not printed.

        These are intentional failure fixtures; letting the report through made
        the release console claim wheels were broken when only a fixture was.
        """
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = check_wheel_contents.main(argv)
        return int(code), output.getvalue()

    def test_an_empty_dist_directory_reports_clearly(self) -> None:
        self.assertIsNone(check_wheel_contents.newest_wheel(self.root / "dist"))
        code, report = self._run_gate(["--root", str(self.root)])
        self.assertEqual(code, 1, report)
        self.assertIn("no wheel found in dist/", report)

    def test_the_gate_exits_non_zero_on_a_bad_wheel(self) -> None:
        wheel = self.build_wheel(
            (*self.complete_names(), "karox/markdown_render.py")
        )
        code, report = self._run_gate(
            ["--root", str(self.root), "--wheel", str(wheel)]
        )
        self.assertEqual(code, 1, report)
        self.assertIn("ships karox/markdown_render.py", report)

    def test_the_gate_exits_zero_on_a_good_wheel(self) -> None:
        wheel = self.build_wheel(self.complete_names())
        code, report = self._run_gate(
            ["--root", str(self.root), "--wheel", str(wheel)]
        )
        self.assertEqual(code, 0, report)
        self.assertIn("wheel matches source", report)


if __name__ == "__main__":
    unittest.main()
