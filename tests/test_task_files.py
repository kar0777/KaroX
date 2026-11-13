from __future__ import annotations

from pathlib import Path

from karox import task_files

TASK = """Scope

* `src/pkg/__init__.py`
* possibly `src/pkg/engine.py`
* see ./docs/notes.md and https://cdn.example.com/examples/jsm/controls/Orbit.js
"""


def _repo(tmp_path: Path) -> Path:
    for rel in ("src/pkg/__init__.py", "src/pkg/engine.py", "docs/notes.md"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return tmp_path


def _call(path: str) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{"name": "repo_read_file", "raw_arguments": f'{{"path": "{path}"}}'}],
    }


def test_paths_written_in_the_task_are_found_without_urls():
    assert task_files.named_paths(TASK) == [
        "src/pkg/__init__.py",
        "src/pkg/engine.py",
        "docs/notes.md",
    ]


def test_a_named_file_the_run_never_opened_is_reported(tmp_path):
    repo = _repo(tmp_path)
    messages = [{"role": "user", "content": TASK}, _call("src/pkg/__init__.py")]
    missed = task_files.unopened(TASK, repo, messages, changed=["docs/notes.md"])
    assert missed == ["src/pkg/engine.py"]
    note = task_files.review_note(missed)
    assert "src/pkg/engine.py" in note and "never opened it" in note


def test_nothing_is_owed_when_every_named_file_was_opened_or_is_absent(tmp_path):
    repo = _repo(tmp_path)
    messages = [_call("src/pkg/__init__.py"), _call("src/pkg/engine.py"), _call("docs/notes.md")]
    assert task_files.unopened(TASK, repo, messages) == []
    assert task_files.unopened("edit lib/missing.py", repo, []) == []
    assert task_files.review_note([]) == ""


def test_root_level_files_the_task_names_are_found() -> None:
    """Audit 2026-11-13: setup.py or index.html in the repo root were never flagged."""

    from karox.task_files import named_paths

    assert named_paths("Fix setup.py and src/pkg/mod.py; open index.html") == [
        "setup.py", "src/pkg/mod.py", "index.html",
    ]


def test_the_unopened_note_comes_before_finish_now() -> None:
    from karox.engine_kernel import _with_review_note
    from karox.prompts import self_review_text

    text = _with_review_note(self_review_text(), "\n7. The task names setup.py")
    assert text.index("7. The task names") < text.index("finish now")
    assert text.rstrip().endswith("finish now with the same answer.")
    assert _with_review_note("a\nb", "") == "a\nb"


def test_a_repeated_long_tool_output_is_sent_once() -> None:
    """Benchmark 2026-11-13: one 35 KB git_diff went out twice on every turn."""

    from karox.engine_provider_bridge import _repeated_output

    shown: dict = {}
    diff = "<returncode>0</returncode>\n<output>\n" + "+line\n" * 1000 + "</output>"
    assert _repeated_output(diff, "git_diff", shown) == diff
    note = _repeated_output(diff, "git_diff", shown)
    assert note is not None and "identical to the output of git_diff" in note and len(note) < 300
    assert _repeated_output("short", "git_diff", shown) == "short"
    assert _repeated_output("short", "git_diff", shown) == "short"
