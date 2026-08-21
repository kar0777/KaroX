"""/new, /resume, and /compact are first-class session commands.

/clear stays log-only per SESSION-MODEL.md; /new adds only screen state on top
of it; /resume is the Session Browser act, typed; /compact is the structured
handoff turned into a bounded continuation preamble. These tests pin the
catalog/routing data and the handoff rendering -- the part that must stay
redacted and bounded.
"""

from karox import tui as karox_tui
from karox.tui import _CONTINUATION_CONTEXT_LIMIT, _continuation_from_handoff


def test_session_commands_are_cataloged_in_both_languages():
    for name in ("/new", "/resume", "/compact"):
        assert name in karox_tui.SLASH_COMMANDS
        assert name in karox_tui._COMMANDS_RU
        assert karox_tui.SLASH_COMMANDS[name].strip()
        assert karox_tui._COMMANDS_RU[name].strip()


def test_session_commands_are_visible_menu_entries():
    for name in ("/new", "/resume", "/compact"):
        assert name in karox_tui.VISIBLE_COMMANDS
    for language in ("en", "ru"):
        heads = {n.split(" ", 1)[0] for n in karox_tui._commands(language)}
        for name in ("/new", "/resume", "/compact"):
            assert name in heads


def test_session_commands_are_interactive_only_in_line_mode():
    for name in ("/new", "/resume", "/compact"):
        assert name in karox_tui._LINE_INTERACTIVE_ONLY


def test_continuation_rendering_selects_and_bounds():
    document = {
        "goal": "wire the effort ladder",
        "constraints": {
            "repository": "D:/projects/KaroX-v5-next",
            "branch": "feat/v5-final-product-pass",
            "access_profile": "workspace_write",
        },
        "summary": "effort ladder wired and tested",
        "changed_files": [f"src/file_{index}.py" for index in range(30)],
        "check_results": [
            {"argv": ["python", "-m", "pytest"], "exit_code": 0},
        ],
        "remaining_steps": ["providers lifecycle", "map depths"],
        "errors": [],
        "unfinished_actions": [],
    }
    text = _continuation_from_handoff(document)
    assert "Goal: wire the effort ladder" in text
    assert "Branch: feat/v5-final-product-pass" in text
    assert "src/file_29.py" in text
    assert "(+10 more)" in text
    assert "python -m pytest -> exit 0" in text
    assert "providers lifecycle" in text


def test_continuation_rendering_tolerates_empty_document():
    assert _continuation_from_handoff({}) == ""


def test_continuation_rendering_is_capped():
    document = {"summary": "x" * (2 * _CONTINUATION_CONTEXT_LIMIT)}
    text = _continuation_from_handoff(document)
    assert len(text) <= _CONTINUATION_CONTEXT_LIMIT


def test_continuation_rendering_skips_blank_and_non_list_fields():
    document = {
        "goal": "  ",
        "constraints": "not-a-mapping",
        "summary": None,
        "changed_files": "not-a-list",
        "check_results": [{"argv": None, "exit_code": 1}],
    }
    text = _continuation_from_handoff(document)
    assert "Goal" not in text
    assert "Repository" not in text
    assert "Changed files" not in text
    assert "? -> exit 1" in text
