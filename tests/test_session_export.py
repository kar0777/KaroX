"""Readable session exports: complete, escaped, redacted and self-contained."""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import pytest

from karox import cli
from karox.models import AccessProfile
from karox.session_export import MAX_BLOCK_CHARACTERS, export_session, timeline
from karox.sessions import SessionStore

SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def _record(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    repository = tmp_path / "repo"
    repository.mkdir()
    record = store.create(repository, "Fix <b>the</b> parser", AccessProfile.WORKSPACE_WRITE, session_id="s-export", name="Parser fix")
    lease = store.acquire(record.session_id, "test")
    record = store.load(record.session_id)
    record.provider_history = [
        {"role": "user", "kind": "task", "content": "Fix <b>the</b> parser"},
        {
            "role": "assistant",
            "content": "Reading the parser first. <script>alert(1)</script>",
            "reasoning_summary": "The bug is probably in tokenize().",
            "tool_calls": [{"call_id": "c1", "name": "read_file", "raw_arguments": '{"path": "parser.py"}'}],
            "model": "demo-model",
            "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "cache_read_tokens": 1000},
            "prompt_cache": {"cause": "tools_changed"},
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "tool_name": "read_file",
            "content": json.dumps({"ok": True, "text": "token = '" + SECRET + "'\n" + "x" * (MAX_BLOCK_CHARACTERS * 3)}),
            "error": False,
        },
        {"role": "tool", "tool_call_id": "c2", "tool_name": "run_check", "content": '{"ok": false}', "error": True},
        {"role": "user", "kind": "engine_note", "content": "Check failed; fix it."},
        {"role": "assistant", "content": "Done.", "tool_calls": [], "model": "demo-model", "usage": {}},
    ]
    record.checks = [
        {"argv": ["python", "-m", "pytest", "-q"], "exit_code": 1, "ok": False},
        {"argv": ["python", "-m", "pytest", "-q"], "exit_code": 0, "ok": True},
    ]
    record.changed_files = ["parser.py"]
    record.usage = {"prompt_tokens": 2400, "completion_tokens": 160, "requests": 2, "costs": {"USD": 0.0123}, "prompt_cache_misses": {"tools_changed": 1}}
    record.summary = "Fixed tokenize()."
    return store.save(record, record.revision, lease), store


def test_html_export_is_complete_escaped_redacted_and_offline(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    page = export_session(record, "html")
    assert page.startswith("<!doctype html>")
    # Content, not markup: nothing from the session can inject tags.
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<b>the</b>" not in page
    assert SECRET not in page
    assert "characters omitted from the export" in page
    # Every entry is on the page, in order.
    assert "Trajectory · 6 entries" in page
    assert page.index("Reading the parser first") < page.index("Result · read_file") < page.index("Done.")
    assert "call · read_file" in page and "reasoning summary" in page
    assert "cache: tools_changed" in page and "in 1200" in page
    assert "passed" in page and "failed" in page and "parser.py" in page
    assert "0.0123 USD" in page and "tools_changed 1" in page
    # Self-contained: no scripts, no external references.
    assert re.search(r"<script|\bsrc=|\bhref=|@import|url\(", page) is None
    assert "Content-Security-Policy" in page


def test_markdown_export_and_timeline(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    items = timeline(record.to_dict())
    assert [item.role for item in items] == ["user", "assistant", "tool", "tool", "system", "assistant"]
    assert items[3].error is True
    text = export_session(record, "md")
    assert text.startswith("# Parser fix")
    assert "## Checks" in text and "`python -m pytest -q` — passed" in text
    assert "Call `read_file`:" in text
    assert SECRET not in text


def test_json_export_is_redacted(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    payload = json.loads(export_session(record, "json"))
    assert payload["session_id"] == "s-export"
    assert SECRET not in json.dumps(payload)
    with pytest.raises(ValueError):
        export_session(record, "pdf")


def test_cli_session_export_writes_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    record, _ = _record(tmp_path)
    monkeypatch.setattr(cli, "session_dir", lambda: tmp_path / "sessions")
    target = tmp_path / "out" / "session.html"
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["session", "export", record.session_id, "--output", str(target), "--json"]) == 0
    payload = json.loads(output.getvalue())
    assert Path(payload["path"]) == target.resolve()
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
