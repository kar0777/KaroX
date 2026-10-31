"""A vision-passed visual.check is browser-runtime proof for a page."""

from __future__ import annotations

import hashlib
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
    core: _Core, record: object, target: str, verdict: str = "pass"
) -> None:
    current = completion_gate.identity(core, record)
    assert current, "the fixture must have a stable identity"
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
                    "completion_target": target,
                    "verdict": verdict,
                    "method": "vision",
                    "page_errors": [],
                    "console_errors": [],
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