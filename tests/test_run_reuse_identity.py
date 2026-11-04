"""Which identity differences may keep an earlier passing run valid."""

from __future__ import annotations

import hashlib
from pathlib import Path

from karox import completion_gate


class _Core:
    def __init__(self, root: Path) -> None:
        self.root = root

    def safe_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError("outside")
        return path


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity(files: dict[str, str], task: str = "fix add", ledger: str = "l1") -> dict:
    return {"files": files, "requirements": ledger, "task": task}


def test_more_files_read_since_the_run_keep_it_valid(tmp_path: Path) -> None:
    prior = _identity({"calc.py": _digest("a")})
    current = _identity({"calc.py": _digest("a"), "notes.md": _digest("n")})
    assert completion_gate.same_run_inputs(_Core(tmp_path), prior, current) is True


def test_a_changed_byte_in_a_bound_file_invalidates_it(tmp_path: Path) -> None:
    prior = _identity({"calc.py": _digest("a")})
    current = _identity({"calc.py": _digest("b")})
    assert completion_gate.same_run_inputs(_Core(tmp_path), prior, current) is False


def test_another_task_never_reuses(tmp_path: Path) -> None:
    prior = _identity({"calc.py": _digest("a")})
    current = _identity({"calc.py": _digest("a")}, task="something else")
    assert completion_gate.same_run_inputs(_Core(tmp_path), prior, current) is False


def test_a_ledger_change_alone_does_not_invalidate_a_command_run(tmp_path: Path) -> None:
    prior = _identity({"calc.py": _digest("a")}, ledger="l1")
    current = _identity({"calc.py": _digest("a")}, ledger="l2")
    assert completion_gate.same_run_inputs(_Core(tmp_path), prior, current) is True


def test_a_bound_file_no_longer_listed_is_rehashed_from_disk(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text("spec", encoding="utf-8")
    prior = _identity({"spec.md": _digest("spec")})
    assert completion_gate.same_run_inputs(_Core(tmp_path), prior, _identity({})) is True
    (tmp_path / "spec.md").write_text("spec v2", encoding="utf-8")
    assert completion_gate.same_run_inputs(_Core(tmp_path), prior, _identity({})) is False


def test_empty_identities_never_match(tmp_path: Path) -> None:
    assert completion_gate.same_run_inputs(_Core(tmp_path), {}, _identity({})) is False
    assert completion_gate.same_run_inputs(_Core(tmp_path), _identity({}), {}) is False
