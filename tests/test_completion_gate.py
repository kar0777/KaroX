"""A vision-passed visual.check is browser-runtime proof for a page."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_a_review_of_old_bytes_is_not_current_after_an_edit(tmp_path: Path) -> None:
    """The change review is bound to the bytes it showed.

    A semantic review (here: the working-tree diff) that predates a later edit
    describes content the user will not receive, so the floor must ask for a
    review of the current bytes instead of accepting the old one.
    """

    record, core = _changed_record(tmp_path)
    reviewed = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result(_complete_review(reviewed)))
    assert "missing current change review" not in completion_gate.floor(core, record)

    (tmp_path / "pkg" / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert "missing current change review" in completion_gate.floor(core, record)


def test_a_folder_without_git_owes_no_change_review(tmp_path: Path) -> None:
    """No start revision means no git.diff can show the change, so none is owed."""

    record, core = _changed_record(tmp_path)
    record.git_baseline = {"version": 1, "kind": "unavailable", "reason": "not-a-repository"}
    reasons = completion_gate.floor(core, record)
    assert not any("change review" in reason for reason in reasons)
    # No git binary at all (TB 4.1 images): no git.diff can run either.
    record.git_baseline = {"version": 1, "kind": "unavailable", "reason": "git-unavailable"}
    assert not any("change review" in reason for reason in completion_gate.floor(core, record))
    assert completion_gate.only_unproducible(["missing current change review", completion_gate.NO_PRODUCER + "current change review"])


_START_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_LATER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"


def _start_baseline(commit: str = _START_COMMIT) -> dict:
    """The durable value SessionStore records for a repository with a commit."""

    return {"version": 1, "kind": "commit", "commit": commit, "captured_at": 1.0}


def _changed_record(
    repository: Path, relative: str = "pkg/mod.py"
) -> tuple[SimpleNamespace, _Core]:
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    record = SimpleNamespace(
        changed_files=[relative],
        task="Fix the units conversion.",
        requirements=[],
        evidence=[],
        provider_history=[],
        git_baseline=_start_baseline(),
    )
    return record, _Core(repository)


def _ignored(**overrides: object) -> dict:
    """The trusted coverage block for known Git-ignored deliverables.

    Core writes it from the session's own changed paths: it checks each one
    against Git's exclude rules (never a tree walk), appends an ignored
    deliverable as a new-file diff and keeps a count, the read state of every
    candidate and an explicit completeness flag.
    """

    ignored: dict = {
        "checked": 0,
        "shown": [],
        "limited": [],
        "excluded": [],
        "missing": [],
        "truncated": False,
        "complete": True,
    }
    ignored.update(overrides)
    return ignored


def _complete_review(identity: dict, **overrides: object) -> dict:
    """A git.diff result whose trusted scope metadata says what it read.

    These fields are produced by Core from the commands it executed; the tests
    vary them only to model results an older Core, a failed command or a
    narrower command would have returned. The base is the session's start
    revision, which is what a current Core records.
    """

    review: dict = {
        "version": 3,
        "mode": "complete",
        "base": "start",
        "base_commit": _START_COMMIT,
        "commands": [["git", "diff", "--no-ext-diff", _START_COMMIT]],
        "paths": [],
        "complete": True,
        "truncated": False,
        "untracked": {
            "listed": 0,
            "shown": [],
            "limited": [],
            "excluded": [],
            "truncated": False,
        },
        "ignored": _ignored(),
    }
    review.update(overrides)
    return {"completion_identity": identity, "review": review}


def _diff_result(data: dict, *, ok: bool = True) -> dict:
    return {
        "role": "tool",
        "core_name": "git.diff",
        "result": {"ok": ok, "requested_by": None, "evidence": [], "data": data},
    }


@pytest.mark.parametrize(
    "review",
    [
        pytest.param({"mode": "worktree"}, id="unstaged-only"),
        pytest.param({"mode": "staged"}, id="staged-only"),
        pytest.param({"mode": "worktree", "paths": ["pkg/mod.py"]}, id="path-filtered"),
        pytest.param({"mode": "complete", "complete": False}, id="scope-incomplete"),
        pytest.param({"mode": "complete", "truncated": True}, id="truncated"),
        pytest.param(
            {"mode": "complete", "paths": ["pkg/mod.py"]}, id="complete-with-filter"
        ),
        pytest.param({"mode": "complete", "base": "worktree"}, id="wrong-base"),
        pytest.param(
            {
                "mode": "complete",
                "untracked": {
                    "listed": 1,
                    "shown": [],
                    "limited": [],
                    "excluded": ["pkg/mod.py"],
                    "truncated": False,
                },
            },
            id="deliverable-excluded-from-the-review",
        ),
        pytest.param({"ignored": None}, id="no-ignored-coverage-metadata"),
        pytest.param(
            {"ignored": _ignored(complete=False, excluded=["pkg/mod.py"])},
            id="ignored-deliverable-denied",
        ),
        pytest.param(
            {"ignored": _ignored(complete=False, missing=["pkg/mod.py"])},
            id="ignored-deliverable-unreadable",
        ),
        pytest.param(
            {"ignored": _ignored(complete=False, truncated=True)},
            id="ignored-check-truncated",
        ),
        pytest.param(
            {"ignored": _ignored(complete=False)}, id="ignored-coverage-incomplete"
        ),
    ],
)
def test_an_incomplete_diff_never_discharges_the_change_review(
    tmp_path: Path, review: dict
) -> None:
    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result(_complete_review(identity, **review)))

    assert "missing current change review" in completion_gate.floor(core, record)


@pytest.mark.parametrize(
    "baseline, review_base",
    [
        pytest.param(_start_baseline(), "start", id="start-revision"),
        pytest.param({"version": 1, "kind": "unborn"}, "unborn", id="unborn"),
    ],
)
def test_a_complete_review_of_the_current_bytes_discharges_the_review(
    tmp_path: Path, baseline: dict, review_base: str
) -> None:
    record, core = _changed_record(tmp_path)
    record.git_baseline = baseline
    identity = completion_gate.identity(core, record)
    record.provider_history.append(
        _diff_result(
            _complete_review(
                identity,
                base=review_base,
                base_commit=_START_COMMIT if review_base == "start" else None,
                untracked={
                    "listed": 1,
                    "shown": ["pkg/mod.py"],
                    "limited": [],
                    "excluded": [],
                    "truncated": False,
                },
            )
        )
    )

    assert "missing current change review" not in completion_gate.floor(core, record)


def test_an_ignored_deliverable_the_review_shows_closes_the_review(
    tmp_path: Path,
) -> None:
    """Covering the ignored deliverable is what discharges the obligation.

    A build or dependency directory the run did not touch still reviews
    (``test_an_excluded_path_the_run_did_not_change_still_reviews``). Here the
    run's own deliverable is the ignored path, and the trusted block says Core
    showed it, so the review is complete.
    """

    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(
        _diff_result(
            _complete_review(
                identity,
                ignored=_ignored(checked=1, shown=["pkg/mod.py"]),
            )
        )
    )

    assert "missing current change review" not in completion_gate.floor(core, record)


@pytest.mark.parametrize(
    "ignored",
    [
        pytest.param(_ignored(checked=1, limited=["pkg/mod.py"]), id="limited-marker"),
        pytest.param(
            _ignored(
                checked=2,
                shown=["pkg/mod.py"],
                limited=["docs/plot.generated.png"],
            ),
            id="shown-and-limited",
        ),
    ],
)
def test_a_limited_ignored_deliverable_is_covered_by_its_marker(
    tmp_path: Path, ignored: dict
) -> None:
    """The established oversized/binary marker policy is coverage, not silence."""

    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(
        _diff_result(_complete_review(identity, ignored=ignored))
    )

    assert "missing current change review" not in completion_gate.floor(core, record)


def test_a_version_two_review_is_not_the_current_contract(tmp_path: Path) -> None:
    """A review persisted before the ignored-deliverable contract cannot close.

    The earlier metadata has no ignored block, so it cannot say whether a
    deliverable Git hides was covered. The run takes a fresh review instead of
    treating the older shape as complete.
    """

    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    review = _complete_review(identity)
    review["review"]["version"] = 2
    record.provider_history.append(_diff_result(review))

    assert "missing current change review" in completion_gate.floor(core, record)


def test_an_excluded_path_the_run_did_not_change_still_reviews(tmp_path: Path) -> None:
    """Only the run's own excluded deliverable blocks the review.

    A repository can hold unrelated untracked files inside ignored directories
    (a dependency tree, a build output). The review names them as excluded; a
    file this run never touched must not keep every run in that repository
    unverified.
    """

    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(
        _diff_result(
            _complete_review(
                identity,
                untracked={
                    "listed": 2,
                    "shown": ["pkg/mod.py"],
                    "limited": [],
                    "excluded": ["node_modules/left-pad/index.js"],
                    "truncated": False,
                },
            )
        )
    )

    assert "missing current change review" not in completion_gate.floor(core, record)


def test_a_failed_diff_is_never_a_review(tmp_path: Path) -> None:
    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result(_complete_review(identity), ok=False))

    assert "missing current change review" in completion_gate.floor(core, record)


def test_a_legacy_diff_without_scope_metadata_is_not_a_review(tmp_path: Path) -> None:
    """A persisted result from before the scope field cannot claim coverage.

    The gate must fail closed on evidence it cannot read, or a resumed old
    session could close on a partial diff it recorded before this change.
    """

    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result({"completion_identity": identity}))

    assert "missing current change review" in completion_gate.floor(core, record)


_NO_TEST_EDITS_TASK = (
    "Complete the task below. Do not modify tests, the public check, or any "
    "grading files.\n\nThis runs the existing tests in `testing/test_mark.py`. "
    "Changes should be made in the source tree, not in the test files."
)


def _start_review(identity: dict, commit: str = _START_COMMIT, **overrides: object) -> dict:
    """A review Core took against one explicit start revision."""

    arguments: dict = {
        "base_commit": commit,
        "commands": [["git", "diff", "--no-ext-diff", commit]],
    }
    arguments.update(overrides)
    return _complete_review(identity, **arguments)


def _legacy_review(identity: dict) -> dict:
    """The version-1 shape Mission 03 wrote: a symbolic ``HEAD`` base.

    It records which command ran, not which tree it read. That is the shape a
    session persisted before this change contains, and the gate has to reject
    it: an elevated commit during the run moves HEAD away from the start, so a
    review of "HEAD" cannot prove which span it showed.
    """

    review = {
        "version": 1,
        "mode": "complete",
        "base": "HEAD",
        "paths": [],
        "complete": True,
        "truncated": False,
        "untracked": {
            "listed": 1,
            "shown": ["pkg/mod.py"],
            "limited": [],
            "excluded": [],
            "truncated": False,
        },
    }
    return {"completion_identity": identity, "review": review}


def test_a_review_against_the_durable_start_revision_closes_the_review(
    tmp_path: Path,
) -> None:
    """The positive half of the contract: a start-relative review is current."""

    record, core = _changed_record(tmp_path)
    record.git_baseline = _start_baseline()
    identity = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result(_start_review(identity)))

    assert "missing current change review" not in completion_gate.floor(core, record)


def test_a_head_relative_review_is_not_a_review_against_the_start_revision(
    tmp_path: Path,
) -> None:
    """An old-style review of "whatever HEAD is now" cannot close the floor.

    This is the hole Mission 03 left open: the run's start revision is ``A``,
    the model closed the obligation with a ``HEAD``-relative diff after an
    elevated commit moved HEAD to ``C``, and that diff showed nothing. The
    durable baseline is what makes the review's scope checkable, so a review
    that never named it -- the version-1 shape -- is not evidence of coverage.
    """

    record, core = _changed_record(tmp_path)
    record.git_baseline = _start_baseline()
    identity = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result(_legacy_review(identity)))

    assert "missing current change review" in completion_gate.floor(core, record)


def test_a_review_taken_at_a_later_head_cannot_close_the_start_baseline(
    tmp_path: Path,
) -> None:
    """A review of the moved tree is scoped to the wrong revision.

    HEAD advanced from ``A`` to ``C`` during the run. A review relative to
    ``C`` can be internally complete and carry the current completion identity
    and still describe the wrong span, so the gate compares the base the review
    names with the durable one.
    """

    record, core = _changed_record(tmp_path)
    record.git_baseline = _start_baseline(_START_COMMIT)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(
        _diff_result(_start_review(identity, commit=_LATER_COMMIT))
    )

    assert "missing current change review" in completion_gate.floor(core, record)


def test_a_legacy_review_without_a_durable_baseline_is_not_complete(
    tmp_path: Path,
) -> None:
    """A session written before this field existed cannot be certified.

    The record has no start revision, so the gate cannot tell which span a
    review covered. It must not invent one from the current HEAD -- that would
    present a moved tree as the original one -- and it must not accept the
    version-1 shape as a substitute.
    """

    record, core = _changed_record(tmp_path)
    identity = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result(_legacy_review(identity)))

    assert "missing current change review" in completion_gate.floor(core, record)


@pytest.mark.parametrize(
    "baseline",
    [
        pytest.param({"version": 1, "kind": "commit", "commit": "HEAD"}, id="a-ref-name"),
        pytest.param({"version": 1, "kind": "commit", "commit": "0123456"}, id="short-sha"),
        pytest.param({"version": 1, "kind": "commit"}, id="no-commit-field"),
        pytest.param({"version": 1, "kind": "unknown"}, id="unknown-kind"),
        pytest.param({"version": 2, "kind": "commit", "commit": _START_COMMIT}, id="future-version"),
        pytest.param({"kind": "commit", "commit": _START_COMMIT}, id="no-version"),
        pytest.param("commit", id="not-a-mapping"),
    ],
)
def test_a_corrupt_start_revision_cannot_close_a_complete_review(
    tmp_path: Path, baseline: object
) -> None:
    """A baseline is either an exact object id or it is not a baseline.

    Corrupt or unknown metadata fails closed instead of being coerced into
    something that would let a review through.
    """

    record, core = _changed_record(tmp_path)
    record.git_baseline = baseline
    identity = completion_gate.identity(core, record)
    record.provider_history.append(_diff_result(_start_review(identity)))

    assert "missing current change review" in completion_gate.floor(core, record)


def test_an_unborn_start_revision_matches_only_an_unborn_review(
    tmp_path: Path,
) -> None:
    """A first-commit workflow stays valid while the repository has no commit.

    The durable baseline says "unborn"; the review Core takes in that state
    names the same thing. A review that claims a start commit instead belongs
    to a different repository state and must not close the floor.
    """

    record, core = _changed_record(tmp_path)
    record.git_baseline = {"version": 1, "kind": "unborn", "captured_at": 1.0}
    identity = completion_gate.identity(core, record)
    record.provider_history.append(
        _diff_result(
            _start_review(
                identity,
                base="unborn",
                base_commit=None,
                untracked={
                    "listed": 1,
                    "shown": ["pkg/mod.py"],
                    "limited": [],
                    "excluded": [],
                    "truncated": False,
                },
            )
        )
    )

    assert "missing current change review" not in completion_gate.floor(core, record)

    record.provider_history[-1] = _diff_result(_start_review(identity))
    assert "missing current change review" in completion_gate.floor(core, record)


def test_a_read_only_session_without_a_baseline_owes_no_review(
    tmp_path: Path,
) -> None:
    """Read-only sessions keep working: with no changed file there is no review.

    The fail-closed baseline rule is about proving coverage of a change. A run
    that changed nothing inherits nothing to prove.
    """

    record, core = _changed_record(tmp_path)
    record.changed_files = []

    assert "missing current change review" not in completion_gate.floor(core, record)


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
    # Restoring an outdated expectation must not read as an order to undo the fix.
    assert "keep the fix" in owed[0] and "superseded_tests" in owed[0]


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


def _svg_record(repository: Path, *, verdict: str) -> tuple[object, _Core]:
    picture = repository / "heron.svg"
    picture.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><rect/></svg>",
        encoding="utf-8",
    )
    record = SimpleNamespace(
        changed_files=["heron.svg"],
        task="Generate an SVG of a heron riding a scooter. Save it as heron.svg.",
        requirements=[],
        evidence=[],
        provider_history=[],
    )
    core = _Core(repository)
    current = completion_gate.identity(core, record)
    record.evidence.append(
        {"kind": "evidence", "evidence_id": "ev-svg", "metadata": {"completion_identity": current}}
    )
    record.provider_history.append(
        {
            "role": "tool",
            "core_name": "visual.check",
            "result": {
                "ok": True,
                "requested_by": None,
                "evidence": [{"evidence_id": "ev-svg"}],
                "data": {
                    "completion_identity": current,
                    "completion_target": "heron.svg",
                    "verdict": verdict,
                    "method": "vision",
                    "page_errors": [],
                    "console_errors": [],
                },
            },
        }
    )
    return record, core


def test_a_vision_pass_on_a_standalone_svg_is_its_browser_proof(tmp_path: Path) -> None:
    """The pelican run (2026-11-05) passed visual.check on its SVG and still
    ended ``verification_failed``: proof was only recognised for HTML pages."""

    record, core = _svg_record(tmp_path, verdict="pass")

    reasons = completion_gate.floor(core, record)

    assert "missing browser runtime evidence" not in reasons
    assert "missing behavioral evidence" not in reasons


def test_a_standalone_svg_without_a_pass_still_owes_browser_proof(tmp_path: Path) -> None:
    record, core = _svg_record(tmp_path, verdict="needs_work")

    reasons = completion_gate.floor(core, record)

    assert "missing browser runtime evidence" in reasons
