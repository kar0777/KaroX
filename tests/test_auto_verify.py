from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from karox import auto_verify

pytest.importorskip("ruff")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

COMMITTED = "import os\n\n\ndef value():\n    return 1\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _repository(root: Path) -> Path:
    package = root / "pkg"
    package.mkdir()
    (package / "mod.py").write_text(COMMITTED, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "-m",
        "init",
    )
    return package / "mod.py"


def _ruff(root: Path, relative: str) -> auto_verify.QuickCheck:
    checks = auto_verify.quick_checks(root, [relative], timeout_s=20.0)
    by_name = {check.name: check for check in checks}
    assert "ruff" in by_name, checks
    return by_name["ruff"]


def test_ruff_does_not_charge_the_change_for_a_pre_existing_issue(tmp_path: Path) -> None:
    """swe-requests had an unused import long before the edit.

    Reporting it as a failed quick check kept the completion floor shut on a
    correct patch. The edit moves the old issue down two lines; it is still
    the same issue.
    """

    module = _repository(tmp_path)
    module.write_text(
        '"""Doc."""\n\n' + COMMITTED + "\n\ndef other():\n    return 2\n",
        encoding="utf-8",
    )

    check = _ruff(tmp_path, "pkg/mod.py")

    assert check.ok is True
    assert "pre-existing" in check.summary


def test_ruff_still_reports_an_issue_the_change_introduced(tmp_path: Path) -> None:
    module = _repository(tmp_path)
    module.write_text(COMMITTED.replace("import os\n", "import os\nimport sys\n"), encoding="utf-8")

    check = _ruff(tmp_path, "pkg/mod.py")

    assert check.ok is False
    assert check.summary.startswith("1 new issues: F401 line 2")
    assert "1 pre-existing not counted" in check.summary


def test_ruff_counts_every_issue_of_a_file_git_does_not_know(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "pkg" / "fresh.py").write_text("import json\n", encoding="utf-8")

    check = _ruff(tmp_path, "pkg/fresh.py")

    assert check.ok is False
    assert check.summary.startswith("1 new issues: F401 line 1")


def test_ruff_outside_git_reports_every_issue(tmp_path: Path) -> None:
    (tmp_path / "loose.py").write_text("import json\n", encoding="utf-8")

    check = _ruff(tmp_path, "loose.py")

    assert check.ok is False
    assert check.summary.startswith("1 issues: F401 line 1")
