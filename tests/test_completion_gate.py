"""A vision-passed visual.check is browser-runtime proof for a page."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

from karox import completion_gate

TASK = "Build a three.js aquarium page with exactly three clownfish."


class _Core:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.verification_commands: tuple[tuple[str, ...], ...] = ()
        self.verification_command_order: tuple[tuple[str, ...], ...] = ()

    def safe_path(self, relative: str) -> Path:
        return (self.repository / relative).resolve()

    def tools(self) -> list[object]:
        return []


def _record(repository: Path, *, verdict: str) -> tuple[object, _Core]:
    page = repository / "aquarium.html"
    page.write_text("<html><body><canvas></canvas></body></html>", encoding="utf-8")
    record = SimpleNamespace(
        changed_files=["aquarium.html"],
        task=TASK,
        requirements=[],
        evidence=[],
        provider_history=[],
    )
    core = _Core(repository)
    current = completion_gate.identity(core, record)
    digest = hashlib.sha256(page.read_bytes()).hexdigest()
    assert current["files"] == {"aquarium.html": digest}
    record.evidence.append(
        {
            "kind": "evidence",
            "evidence_id": "ev-visual",
            "metadata": {"completion_identity": current},
        }
    )
    record.provider_history.append(
        {
            "role": "tool",
            "core_name": "visual.check",
            "result": {
                "ok": True,
                "requested_by": None,
                "evidence": [{"evidence_id": "ev-visual"}],
                "data": {
                    "completion_identity": current,
                    "completion_target": "aquarium.html",
                    "verdict": verdict,
                    "method": "vision",
                    "page_errors": [],
                    "console_errors": [],
                },
            },
        }
    )
    return record, core


def test_a_vision_pass_on_the_changed_page_discharges_browser_evidence(
    tmp_path: Path,
) -> None:
    record, core = _record(tmp_path, verdict="pass")

    reasons = completion_gate.floor(core, record)

    assert "missing browser runtime evidence" not in reasons


def test_needs_work_still_owes_browser_evidence(tmp_path: Path) -> None:
    record, core = _record(tmp_path, verdict="needs_work")

    reasons = completion_gate.floor(core, record)

    assert "missing browser runtime evidence" in reasons


def _add_visual(
    core: _Core,
    record: object,
    target: str,
    verdict: str = "pass",
    *,
    console_errors: tuple[str, ...] = (),
) -> None:
    current = completion_gate.identity(core, record)
    assert current, "the fixture must have a stable identity"
    evidence_id = f"ev-visual-{len(record.evidence)}"
    record.evidence.append(
        {
            "kind": "evidence",
            "evidence_id": evidence_id,
            "metadata": {"completion_identity": current},
        }
    )
    record.provider_history.append(
        {
            "role": "tool",
            "core_name": "visual.check",
            "result": {
                "ok": True,
                "requested_by": None,
                "evidence": [{"evidence_id": evidence_id}],
                "data": {
                    "completion_identity": current,
                    "completion_target": target,
                    "verdict": verdict,
                    "method": "vision",
                    "page_errors": [],
                    "console_errors": list(console_errors),
                },
            },
        }
    )


def _add_visual_pass(core: _Core, record: object, target: str) -> None:
    _add_visual(core, record, target, "pass")


def test_an_unchanged_scaffold_page_proves_a_changed_script(tmp_path: Path) -> None:
    """The page is the proof surface even when only its script changed."""

    (tmp_path / "index.html").write_text(
        '<html><body><canvas></canvas><script src="game.js"></script></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "game.js").write_text("console.log('game');\n", encoding="utf-8")
    (tmp_path / "probe_snake.cjs").write_text("// scratch probe\n", encoding="utf-8")
    record = SimpleNamespace(
        changed_files=["game.js", "probe_snake.cjs"],
        task="Complete the canvas snake game.",
        requirements=[],
        evidence=[],
        provider_history=[],
    )
    core = _Core(tmp_path)
    _add_visual_pass(core, record, "index.html")

    reasons = completion_gate.floor(core, record)

    assert "missing browser runtime evidence" not in reasons


def test_an_unrelated_page_does_not_prove_a_changed_script(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html><body>Index</body></html>", encoding="utf-8")
    (tmp_path / "other.js").write_text("console.log('other');\n", encoding="utf-8")
    record = SimpleNamespace(
        changed_files=["other.js"],
        task="Complete the canvas snake game.",
        requirements=[],
        evidence=[],
        provider_history=[],
    )
    core = _Core(tmp_path)
    _add_visual_pass(core, record, "index.html")

    reasons = completion_gate.floor(core, record)

    assert "missing browser runtime evidence" in reasons


def _probe_record(tmp_path: Path) -> tuple[object, _Core]:
    (tmp_path / "index.html").write_text(
        "<html><body><canvas></canvas></body></html>", encoding="utf-8"
    )
    (tmp_path / "_probe.html").write_text(
        "<html><body>probe</body></html>", encoding="utf-8"
    )
    record = SimpleNamespace(
        changed_files=["index.html", "_probe.html"],
        task="Build a three.js game with a visible track, obstacles and coins.",
        requirements=[],
        evidence=[],
        provider_history=[],
    )
    return record, _Core(tmp_path)


def test_a_failed_scratch_probe_is_not_an_active_failed_check(
    tmp_path: Path,
) -> None:
    """A diagnostic page must not stay owed against the deliverable."""

    record, core = _probe_record(tmp_path)
    _add_visual(core, record, "_probe.html", "needs_work")

    reasons = completion_gate.floor(core, record)

    assert not any("active failed check" in reason for reason in reasons)
    assert "missing visual evidence" in reasons


def test_a_passing_scratch_probe_does_not_prove_the_deliverable(
    tmp_path: Path,
) -> None:
    record, core = _probe_record(tmp_path)
    _add_visual(core, record, "_probe.html", "pass")

    reasons = completion_gate.floor(core, record)

    assert "missing visual evidence" in reasons


def test_identity_ignores_scratch_cleanup(tmp_path: Path) -> None:
    """Deleting a probe must not make a pass on the deliverable stale."""

    record, core = _probe_record(tmp_path)
    (tmp_path / "_scratch_check.py").write_text("print('probe')\n", encoding="utf-8")
    record.changed_files.append("_scratch_check.py")

    before = completion_gate.identity(core, record)
    (tmp_path / "_probe.html").unlink()
    (tmp_path / "_scratch_check.py").unlink()
    after = completion_gate.identity(core, record)

    assert before == after
    assert "_probe.html" not in before["files"]
    assert "_scratch_check.py" not in before["files"]
    assert "index.html" in before["files"]


def test_identity_ignores_the_runs_own_unreferenced_harness_page(tmp_path: Path) -> None:
    """Editing ``selftest.html`` that nothing links must not stale the game's proof."""

    record, core = _probe_record(tmp_path)
    (tmp_path / "selftest.html").write_text("<p>PASS 1</p>", encoding="utf-8")
    record.changed_files.append("selftest.html")
    record.created_files = ["selftest.html"]

    before = completion_gate.identity(core, record)
    (tmp_path / "selftest.html").write_text("<p>PASS 1</p><p>PASS 2</p>", encoding="utf-8")
    after = completion_gate.identity(core, record)

    assert before == after and "selftest.html" not in before["files"]
    # Once the deliverable links it, it is delivered and bound again.
    (tmp_path / "index.html").write_text('<a href="selftest.html">tests</a>', encoding="utf-8")
    linked = completion_gate.identity(core, record)
    assert "selftest.html" in linked["files"]


def _mutation(core_name: str, data: dict) -> dict:
    return {"role": "tool", "core_name": core_name, "result": {"ok": True, "data": data}}


def test_identity_ignores_deleting_the_runs_own_harness_page(tmp_path: Path) -> None:
    """Deleting a harness page the run created must not stale the game's proof.

    ``created_files`` forgets a deleted file. A measured run deleted its
    ``self-test.html`` as told, the page stopped being diagnostic, entered the
    identity as deleted and staled every proof and attestation.
    """

    record, core = _probe_record(tmp_path)
    (tmp_path / "self-test.html").write_text("<p>PASS 1</p>", encoding="utf-8")
    record.changed_files.append("self-test.html")
    record.created_files = ["self-test.html"]
    record.provider_history.append(
        _mutation(
            "repo.write_file",
            {"path": "self-test.html", "changed": True, "previous_sha256": None},
        )
    )
    before = completion_gate.identity(core, record)

    (tmp_path / "self-test.html").unlink()
    record.created_files = []
    record.provider_history.append(
        _mutation(
            "repo.apply_patch",
            {
                "changed": True,
                "path": "self-test.html",
                "post_state": {"self-test.html": None},
                "operations": [
                    {"op": "delete", "path": "self-test.html", "changed": True,
                     "previous_sha256": "abc", "sha256": None}
                ],
            },
        )
    )
    after = completion_gate.identity(core, record)

    assert before == after and "self-test.html" not in after["files"]
    assert completion_gate.diagnostic_paths(core, record) == frozenset({"self-test.html"})


def test_a_pass_on_the_runs_own_harness_page_does_not_prove_the_deliverable(
    tmp_path: Path,
) -> None:
    """Only the page the user opens can carry the visual proof.

    A measured run spent its last visual checks on its own ``self-test.html``;
    a pass there must not stand in for the game page.
    """

    record, core = _probe_record(tmp_path)
    (tmp_path / "self-test.html").write_text("<p>PASS 1</p>", encoding="utf-8")
    record.changed_files.append("self-test.html")
    record.created_files = ["self-test.html"]
    _add_visual(core, record, "self-test.html", "pass")

    assert completion_gate.visual_proven(core, record) is False
    assert "missing visual evidence" in completion_gate.floor(core, record)

    _add_visual(core, record, "index.html", "pass")
    assert completion_gate.visual_proven(core, record) is True


def test_identity_binds_a_deleted_page_the_run_did_not_create(tmp_path: Path) -> None:
    """Deleting a page that was there before the run is a real change."""

    record, core = _probe_record(tmp_path)
    (tmp_path / "about.html").write_text("<p>about</p>", encoding="utf-8")
    record.changed_files.append("about.html")
    record.created_files = []
    before = completion_gate.identity(core, record)

    (tmp_path / "about.html").unlink()
    record.provider_history.append(
        _mutation(
            "repo.apply_patch",
            {
                "changed": True,
                "operations": [
                    {"op": "delete", "path": "about.html", "changed": True,
                     "previous_sha256": "abc", "sha256": None}
                ],
            },
        )
    )
    after = completion_gate.identity(core, record)

    assert after["files"]["about.html"] == "missing" and before != after
    assert completion_gate.diagnostic_paths(core, record) == frozenset()


def test_a_later_rejudged_fail_does_not_erase_a_pass_on_same_bytes(
    tmp_path: Path,
) -> None:
    record, core = _probe_record(tmp_path)
    _add_visual(core, record, "index.html", "pass")
    _add_visual(core, record, "index.html", "needs_work")

    reasons = completion_gate.floor(core, record)

    assert not any("active failed check" in reason for reason in reasons)
    assert "missing visual evidence" not in reasons


def test_a_fail_after_an_edit_is_still_active(tmp_path: Path) -> None:
    record, core = _probe_record(tmp_path)
    _add_visual(core, record, "index.html", "pass")
    (tmp_path / "index.html").write_text(
        "<html><body><canvas id='new'></canvas></body></html>", encoding="utf-8"
    )
    _add_visual(core, record, "index.html", "needs_work")

    reasons = completion_gate.floor(core, record)

    assert any("active failed check" in reason for reason in reasons)
    assert "missing visual evidence" in reasons


def test_an_unavailable_visual_check_is_not_a_failed_check(
    tmp_path: Path,
) -> None:
    record, core = _probe_record(tmp_path)
    _add_visual(core, record, "index.html", "unavailable")

    reasons = completion_gate.floor(core, record)

    assert not any("active failed check" in reason for reason in reasons)
    assert "missing visual evidence" in reasons


def test_a_later_fail_with_console_errors_overrides_an_earlier_pass(
    tmp_path: Path,
) -> None:
    record, core = _probe_record(tmp_path)
    _add_visual(core, record, "index.html", "pass")
    _add_visual(
        core, record, "index.html", "needs_work", console_errors=("boom",)
    )

    reasons = completion_gate.floor(core, record)

    assert any("active failed check" in reason for reason in reasons)


def test_apply_patch_post_state_is_current_structural_evidence(tmp_path: Path) -> None:
    """An atomic patch reports per-file digests; the gate must read them.

    swe-sympy-24213 removed its scratch code from si.py with apply_patch. The
    patch compiled, but the result carried ``post_state`` instead of
    ``content_sha256``, so the gate declared the file without structural
    evidence and failed a correct, grader-approved patch.
    """

    target = tmp_path / "pkg" / "mod.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    record = SimpleNamespace(
        changed_files=["pkg/mod.py"],
        task="Fix the units conversion.",
        requirements=[],
        evidence=[],
        provider_history=[
            {
                "role": "tool",
                "core_name": "repo.apply_patch",
                "result": {
                    "ok": True,
                    "data": {
                        "changed": True,
                        "path": "pkg/mod.py",
                        "post_state": {"pkg/mod.py": digest},
                        "quick_checks": [
                            {"name": "py_compile", "ok": True, "summary": "ok"}
                        ],
                    },
                },
            }
        ],
    )
    core = _Core(tmp_path)

    reasons = completion_gate.floor(core, record)

    assert "missing current structural evidence: pkg/mod.py" not in reasons


def _task_record(task: str, changed_files: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        task=task,
        requirements=[],
        changed_files=list(changed_files or ["pkg/mod.py"]),
        evidence=[],
        provider_history=[],
    )


_SOURCE_SCOPE_TASK = (
    "This workspace contains the Requests source tree. From the workspace root run:\n\n"
    "    python run_public_tests.py\n\n"
    "This runs the existing tests in `tests/test_utils.py`. "
    "Changes should be made in the source tree, not in the test files."
)


def test_a_source_scope_instruction_is_not_a_test_change_request() -> None:
    """SWE task text says where changes go; it does not ask for test edits.

    swe-requests-6028 ends with "Changes should be made in the source tree, not
    in the test files." The prefix-only negation rule did not see the trailing
    "not in the test files", so every run was forced into a repair turn that
    modified tests the task had just placed out of scope.
    """

    record = _task_record(_SOURCE_SCOPE_TASK)

    assert completion_gate._explicit_test_change_required(record) is False


def test_a_source_scope_instruction_does_not_owe_a_test_change(tmp_path: Path) -> None:
    target = tmp_path / "pkg" / "mod.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")
    record = _task_record(_SOURCE_SCOPE_TASK)
    core = _Core(tmp_path)

    reasons = completion_gate.floor(core, record)

    assert "explicit test change requested but no test file changed" not in reasons


def test_a_real_test_request_still_owes_a_test_change(tmp_path: Path) -> None:
    target = tmp_path / "pkg" / "mod.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")
    record = _task_record(
        "Implement the fix in the source tree. "
        "Add a regression test in tests/test_proxy_auth.py."
    )
    core = _Core(tmp_path)

    reasons = completion_gate.floor(core, record)

    assert "explicit test change requested but no test file changed" in reasons


def test_postfix_scope_negations_are_not_test_requests() -> None:
    for task in (
        "Make changes in the source tree rather than in the tests.",
        "Keep the change outside the test files.",
        "You should not modify tests for this task.",
    ):
        assert (
            completion_gate._explicit_test_change_required(_task_record(task)) is False
        ), task


def test_a_scope_sentence_does_not_borrow_a_later_test_constraint() -> None:
    task = (
        "Changes should be made in the source tree, not in the test files. "
        "The existing tests must keep passing."
    )

    assert completion_gate._explicit_test_change_required(_task_record(task)) is False


def test_a_negated_request_does_not_swallow_a_later_one() -> None:
    for task in (
        "Do not modify the existing tests. Add a regression test for the new branch.",
        "Do not change tests; add a new test file instead.",
    ):
        assert (
            completion_gate._explicit_test_change_required(_task_record(task)) is True
        ), task


def test_a_stale_patch_digest_does_not_prove_the_current_file(tmp_path: Path) -> None:
    target = tmp_path / "pkg" / "mod.py"
    target.parent.mkdir()
    target.write_text("VALUE = 2\n", encoding="utf-8")
    record = SimpleNamespace(
        changed_files=["pkg/mod.py"],
        task="Fix the units conversion.",
        requirements=[],
        evidence=[],
        provider_history=[
            {
                "role": "tool",
                "core_name": "repo.apply_patch",
                "result": {
                    "ok": True,
                    "data": {
                        "changed": True,
                        "post_state": {"pkg/mod.py": "0" * 64},
                        "quick_checks": [
                            {"name": "py_compile", "ok": True, "summary": "ok"}
                        ],
                    },
                },
            }
        ],
    )
    core = _Core(tmp_path)

    reasons = completion_gate.floor(core, record)

    assert "missing current structural evidence: pkg/mod.py" in reasons

_NO_TEST_EDITS_TASK = (
    "Complete the task below. Do not modify tests, the public check, or any "
    "grading files.\n\nThis runs the existing tests in `testing/test_mark.py`. "
    "Changes should be made in the source tree, not in the test files."
)


def test_a_test_edit_ban_is_read_from_the_task() -> None:
    for task, banned in (
        (_NO_TEST_EDITS_TASK, True),
        ("Changes should be made in the source tree, not in the test files.", True),
        ("Don't touch the test files; fix the parser.", True),
        ("Не меняйте тесты, исправьте парсер.", True),
        ("Fix the parser. Do not change the test command.", False),
        ("Fix the parser and keep the suite green.", False),
        # Asking for a test change keeps the old handling: no ban.
        ("Do not modify the existing tests. Add a regression test for the new branch.", False),
    ):
        assert bool(completion_gate._test_edit_ban(_task_record(task))) is banned, task


def _git(repository: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
        cwd=repository, check=True, capture_output=True,
    )


def _edited_test_record(tmp_path: Path, *, restored: bool) -> tuple[SimpleNamespace, _Core]:
    original = b"def test_old():\n    assert True\n"
    test_file = tmp_path / "testing" / "test_mark.py"
    test_file.parent.mkdir()
    test_file.write_bytes(original)
    source = tmp_path / "pkg" / "mod.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "start")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    if not restored:
        test_file.write_bytes(original + b"\n\ndef test_new():\n    pass\n")
    record = SimpleNamespace(
        task=_NO_TEST_EDITS_TASK,
        requirements=[],
        changed_files=["pkg/mod.py", "testing/test_mark.py"],
        created_files=[],
        evidence=[],
        provider_history=[
            {
                "role": "tool",
                "core_name": "repo.edit_file",
                "result": {
                    "ok": True,
                    "data": {
                        "path": "testing/test_mark.py",
                        "changed": True,
                        "previous_sha256": hashlib.sha256(original).hexdigest(),
                        "sha256": "later",
                    },
                },
            }
        ],
    )
    return record, _Core(tmp_path)


def test_an_edited_existing_test_blocks_the_finish_when_the_task_forbids_it(tmp_path: Path) -> None:
    record, core = _edited_test_record(tmp_path, restored=False)

    reasons = completion_gate.floor(core, record)

    owed = [reason for reason in reasons if reason.startswith("existing test files changed")]
    assert len(owed) == 1
    assert "testing/test_mark.py" in owed[0]
    assert "Do not modify tests" in owed[0]


def test_a_restored_test_file_no_longer_blocks_the_finish(tmp_path: Path) -> None:
    record, core = _edited_test_record(tmp_path, restored=True)

    reasons = completion_gate.floor(core, record)

    assert not [reason for reason in reasons if reason.startswith("existing test files changed")]


def test_test_files_are_free_when_the_task_does_not_forbid_edits(tmp_path: Path) -> None:
    record, core = _edited_test_record(tmp_path, restored=False)
    record.task = "Fix the mark inheritance in the source tree."

    reasons = completion_gate.floor(core, record)

    assert not [reason for reason in reasons if reason.startswith("existing test files changed")]


def test_a_test_file_the_run_created_is_not_an_edited_existing_test(tmp_path: Path) -> None:
    record, core = _edited_test_record(tmp_path, restored=False)
    record.created_files = ["testing/test_mark.py"]
    record.provider_history[0]["result"]["data"]["previous_sha256"] = None

    reasons = completion_gate.floor(core, record)

    assert not [reason for reason in reasons if reason.startswith("existing test files changed")]


def test_an_untracked_test_artifact_is_not_an_edited_existing_test(tmp_path: Path) -> None:
    record, core = _edited_test_record(tmp_path, restored=True)
    artifact = tmp_path / "_probe" / "__pycache__" / "test_probe.cpython-313.pyc"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"later")
    stray = tmp_path / "tests" / "test_generated.py"
    stray.parent.mkdir()
    stray.write_text("def test_generated():\n    pass\n", encoding="utf-8")
    record.changed_files += [artifact.relative_to(tmp_path).as_posix(), "tests/test_generated.py"]
    for path in record.changed_files[-2:]:
        record.provider_history.append(
            {
                "role": "tool",
                "core_name": "repo.command",
                "result": {
                    "ok": True,
                    "data": {"operations": [{"op": "write", "path": path, "changed": True, "previous_sha256": "0" * 64}]},
                },
            }
        )

    reasons = completion_gate.floor(core, record)

    assert not [reason for reason in reasons if reason.startswith("existing test files changed")]


_PR_TEMPLATE_TASK = (
    "Complete the task below. Do not modify tests, the public check, or any grading "
    "files.\n\nTASK\nConsider MRO when obtaining marks for classes\n\n"
    "- [ ] Include documentation when adding new features.\n"
    "- [ ] Include new tests or update existing tests when applicable.\n\n"
    "This runs the existing tests in `testing/test_mark.py`. "
    "Changes should be made in the source tree, not in the test files."
)


def test_a_quoted_pr_checklist_is_not_an_explicit_test_request() -> None:
    record = _task_record(_PR_TEMPLATE_TASK)

    assert completion_gate._explicit_test_change_required(record) is False
    assert "Do not modify tests" in completion_gate._test_edit_ban(record)


def test_conditional_test_requests_do_not_owe_a_test_change() -> None:
    for task in (
        "Fix the parser. Add tests where appropriate.",
        "Fix the parser and update the tests if needed.",
        "- [x] Add a regression test",
        "Исправьте парсер, при необходимости обновите тесты.",
    ):
        assert completion_gate._explicit_test_change_required(_task_record(task)) is False, task
    assert completion_gate._explicit_test_change_required(
        _task_record("Fix the parser and add a regression test for the empty header.")
    ) is True
