"""Old error paths in edited functions are named in the maintainer review."""

from __future__ import annotations

import subprocess
from pathlib import Path

from karox import review_anchors

SOURCE = '''def pick(mapping, name):
    names = [v for k, v in mapping.items() if k == name]
    if not names:  # pragma: no cover
        raise RuntimeError("type is not found")
    return names[0]


def other():
    raise ValueError("untouched function")
'''


def test_changed_lines_reads_hunks_and_deletions() -> None:
    diff = "+++ b/a.py\n@@ -1,0 +2,3 @@\n+x\n@@ -9 +11,0 @@\n-y\n+++ /dev/null\n@@ -1 +0,0 @@\n"
    assert review_anchors.changed_lines(diff) == {"a.py": {2, 3, 4, 11, 12}}


def test_only_raises_the_edit_did_not_write_in_edited_functions() -> None:
    found = review_anchors.anchors(SOURCE, {2})
    assert found == ["4 `if not names: raise RuntimeError('type is not found')`"]
    assert review_anchors.anchors(SOURCE, {4}) == []
    assert review_anchors.anchors("def broken(:\n", {1}) == []


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True, text=True).stdout


def test_review_note_names_the_old_raise(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "mod.py").write_text(SOURCE, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    commit = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert review_anchors.review_note(tmp_path, commit) == ""
    (tmp_path / "mod.py").write_text(SOURCE.replace("k == name", "k == name or v == name"), encoding="utf-8")
    note = review_anchors.review_note(tmp_path, commit)
    assert "mod.py:4 `if not names: raise RuntimeError" in note
    assert "untouched function" not in note
