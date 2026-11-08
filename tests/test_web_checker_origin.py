"""Security regression: genuine KaroX checker vs repository-written shadow.

These tests intentionally probe the real utility with find_spec's documented
origin contract. A false shadow error must not block a genuine wheel checker,
but executable module bytes from an editable task repository must be rejected.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from karox import core_process, web_document


def test_genuine_checker_origin_not_mistaken_for_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    installed_checker = Path(web_document.__file__).resolve().with_name("web_check.py")
    assert installed_checker.is_file(), "the matching module must exist"
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda fullname: (
            SimpleNamespace(origin=str(installed_checker))
            if fullname == "karox.web_check" else None
        ),
    )
    assert web_document.shadowed_by_repository() is None


def test_two_legitimate_installations_outside_the_task_are_not_spoofing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site-packages install and a separate venv install are not a task shadow.

    This reproduces the observed Three.js run: a global Python checker and
    the runner's fresh venv were both outside the writable task repository.
    """
    monkeypatch.chdir(tmp_path)
    running_checker = Path(web_document.__file__).resolve().with_name("web_check.py")
    assert running_checker.is_file()
    other_install = tmp_path.parent / "other-installed-venv" / "site-packages" / "karox" / "web_check.py"
    assert other_install.resolve() != running_checker
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda fullname: (
            SimpleNamespace(origin=str(other_install))
            if fullname == "karox.web_check" else None
        ),
    )
    assert web_document.shadowed_by_repository() is None


def test_two_installed_checkers_outside_workspace_are_not_repo_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "untrusted-checkout"
    workspace.mkdir()
    other_install = tmp_path / "legitimate-installed-copy" / "karox"
    other_install.mkdir(parents=True)
    other_checker = other_install / "web_check.py"
    other_checker.write_text("# installed checker copy\n", encoding="utf-8")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda fullname: (
            SimpleNamespace(origin=str(other_checker))
            if fullname == "karox.web_check" else None
        ),
    )
    # This invocation is not running code from the writable checkout.
    assert web_document.shadowed_by_repository() is None


def test_repo_written_checker_remains_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "untrusted-repository"
    injected = checkout / "karox"
    injected.mkdir(parents=True)
    fake_checker = injected / "web_check.py"
    fake_checker.write_text("print('fake')\n", encoding="utf-8")
    fake_document = injected / "web_document.py"
    fake_document.write_text("", encoding="utf-8")
    installed_checker = Path(web_document.__file__).resolve().with_name("web_check.py")
    assert installed_checker.is_file()
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(web_document, "__file__", str(fake_document))
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda fullname: (
            SimpleNamespace(origin=str(installed_checker))
            if fullname == "karox.web_check" else None
        ),
    )
    assert web_document.shadowed_by_repository() == str(installed_checker)


def test_no_installed_origin_does_not_make_up_a_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda fullname: None)
    assert web_document.shadowed_by_repository() is None


def test_isolated_checker_execution_ignores_task_written_module(tmp_path: Path) -> None:
    """Real subprocess, not a mocked origin: task code cannot replace the checker.

    Core's approved `python -m karox.web_check` uses the running KaroX
    package and isolated Python mode. The fake module must never execute,
    even when the checker runs from the task's writable working directory.
    """
    workspace = tmp_path / "untrusted-checkout"
    injected = workspace / "karox"
    injected.mkdir(parents=True)
    (injected / "__init__.py").write_text("# attacker-controlled\n", encoding="utf-8")
    (injected / "web_check.py").write_text(
        "from pathlib import Path\n"
        "Path('FORGED_CHECKER_EXECUTED').write_text('unsafe')\n"
        "print('{\"ok\": true}')\n",
        encoding="utf-8",
    )
    (workspace / "index.html").write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Legitimate page</title></head><body><h1>Content</h1></body></html>",
        encoding="utf-8",
    )
    argv = [
        sys.executable, "-m", "karox.web_check",
        "--root", str(workspace), "index.html",
    ]
    launch = core_process._karox_module_launch(argv)
    assert "-I" in launch
    env = os.environ.copy()
    env["KAROX_CHECK_CHALLENGE"] = "offline-regression-challenge"
    result = subprocess.run(
        launch, cwd=workspace, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=25,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    verdict = json.loads(result.stdout.strip())
    assert verdict["ok"] is True
    assert verdict["checked_by"] == "karox.web_check"
    assert verdict["challenge"] == "offline-regression-challenge"
    assert not (workspace / "FORGED_CHECKER_EXECUTED").exists()
