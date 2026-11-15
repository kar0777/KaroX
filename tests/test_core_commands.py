"""The developer-command boundary for git and ordinary checks."""

from __future__ import annotations

import pytest

from karox.core import CoreRuntime, InvalidCommand


def test_a_safe_git_read_is_allowed_in_a_check() -> None:
    assert CoreRuntime._validate_process(["git", "diff", "--check"]) == [
        "git",
        "diff",
        "--check",
    ]


def test_a_git_push_is_still_refused() -> None:
    with pytest.raises(InvalidCommand, match="read-only"):
        CoreRuntime._validate_process(["git", "push", "origin", "main"])

def test_a_check_cannot_kill_processes_by_image_name() -> None:
    with pytest.raises(InvalidCommand, match="image name"):
        CoreRuntime._validate_process(["taskkill", "/IM", "python.exe", "/F"])


def test_a_check_may_stop_one_process_by_pid() -> None:
    argv = ["taskkill", "/PID", "424242", "/T", "/F"]
    assert CoreRuntime._validate_process(argv) == argv


def _rules(*entries):
    from types import SimpleNamespace

    from karox.core_support import VerificationRule

    return SimpleNamespace(_verification_rules=tuple(VerificationRule.parse(e) for e in entries))


def test_an_approved_shell_rule_runs_its_script() -> None:
    core = _rules(["bash", "-lc", "*"])
    script = ["bash", "-lc", "cd /app && sh rebuild_parity_report.sh && legacy-score --failed-logins 3"]
    assert CoreRuntime._shell_rule_matches(core, script)
    assert CoreRuntime._validate_process(script, approved_shell=True) == script
    with pytest.raises(InvalidCommand, match="shell"):
        CoreRuntime._validate_process(["bash", "-lc", "sh x.sh && git push origin main"], approved_shell=True)
    with pytest.raises(InvalidCommand, match="blocked"):
        CoreRuntime._validate_process(script)
    assert not CoreRuntime._shell_rule_matches(_rules(["python", "*"]), script)


def test_a_missed_edit_says_when_it_is_already_applied() -> None:
    from karox.edit_engine import EditOp, _not_found_failure

    text = "def f():\n    return 2\n"
    failure = _not_found_failure(text, 0, EditOp(old="    return 1\n", new="    return 2\n"))
    assert "already be applied" in failure.hint
    other = _not_found_failure(text, 0, EditOp(old="    return 1\n", new="    return 3\n"))
    assert "already be applied" not in other.hint


def test_the_repeat_notice_names_a_loop_at_three_five_and_eight() -> None:
    from karox.engine_guidance import REPEAT_NOTICE_AT, EngineGuidanceMixin

    class _Env(EngineGuidanceMixin):
        pass

    env = _Env.__new__(_Env)
    fired = [n for n in range(1, 10) if env._repeat_notice("repo.read_file", {"path": "a.py"})]
    assert tuple(fired) == REPEAT_NOTICE_AT
    assert env._repeat_notice("repo.read_file", {"path": "b.py"}) == ""


def test_the_environment_block_names_layout_toolchain_and_git(tmp_path) -> None:
    from karox.project_context import render_environment

    (tmp_path / "app").mkdir()
    (tmp_path / "run.sh").write_text("echo hi\n", encoding="utf-8")
    text = render_environment(tmp_path)
    assert "top level: app/, run.sh" in text
    assert "installed:" in text
    assert "git:" in text and "no history" in text


def test_an_early_finisher_is_told_how_much_time_is_left() -> None:
    from types import SimpleNamespace

    from karox.engine_kernel import EngineKernel

    kernel = EngineKernel.__new__(EngineKernel)
    kernel.limits = SimpleNamespace(max_seconds=3600)
    kernel.remaining_seconds = lambda: 3100.0  # type: ignore[method-assign]
    assert "8 of 60 minutes" in kernel._time_used_note()
    kernel.remaining_seconds = lambda: 600.0  # type: ignore[method-assign]
    assert kernel._time_used_note() == ""


def test_a_heredoc_shell_probe_is_an_inline_snippet() -> None:
    from karox.completion_gate import _inline_probe

    assert _inline_probe(["bash", "-lc", "cat > /tmp/p.cc <<'EOF'\nint main(){}\nEOF\ng++ /tmp/p.cc"])
    assert not _inline_probe(["bash", "-lc", "make -C /app test"])
    assert _inline_probe(["python", "-c", "print(1)"])
    assert _inline_probe(["python3", "-u", "-X", "utf8", "-c", "print(1)"])
    assert not _inline_probe(["python3", "-u", "-m", "pytest", "-c", "setup.cfg"])
    assert not _inline_probe(["python", "-c"])
    # Scratch scripts under /tmp (risk-scorer v50) are probes, not deliverable checks.
    assert _inline_probe(["bash", "-lc", "cd /tmp && python3 p8.py | grep extra="])
    assert _inline_probe(["python3", "/tmp/p11.py"])
    assert not _inline_probe(["python3", "scripts/check.py"])
    assert not _inline_probe(["bash", "-lc", "cd /app && python3 -m pytest -q"])
    # Asking a program for its usage is a look, not a check (rebench26 v616 nicegui).
    assert _inline_probe(["python", "nicegui/elements/xterm/xterm.py", "--help"])
    assert _inline_probe(["python", "-m", "pkg.cli", "-h"])
    assert not _inline_probe(["python", "-m", "pkg.cli", "--help-me"])
