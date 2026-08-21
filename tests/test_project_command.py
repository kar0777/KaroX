"""/project is the workspace act, typed.

Without an argument it opens the same manager Ctrl+W opens; with an argument it
switches by registered project id or approved path, and unknown targets fall
through to the /workspace path rules. These tests pin the catalog/routing data
and the pure resolution helper.
"""

from karox import tui as karox_tui
from karox.project_registry import ProjectRegistry
from karox.tui import _resolve_project_target


def test_project_is_cataloged_in_both_languages():
    assert "/project" in karox_tui.SLASH_COMMANDS
    assert "/project" in karox_tui._COMMANDS_RU
    assert karox_tui.SLASH_COMMANDS["/project"].strip()
    assert karox_tui._COMMANDS_RU["/project"].strip()


def test_project_is_a_visible_menu_entry():
    assert "/project" in karox_tui.VISIBLE_COMMANDS
    for language in ("en", "ru"):
        heads = {n.split(" ", 1)[0] for n in karox_tui._commands(language)}
        assert "/project" in heads


def test_project_is_interactive_only_in_line_mode():
    assert "/project" in karox_tui._LINE_INTERACTIVE_ONLY


def test_resolve_project_target_by_id_and_path(tmp_path):
    registry = ProjectRegistry.single(tmp_path)
    entry = registry.default
    assert entry is not None
    assert _resolve_project_target(registry, entry.project_id) == str(entry.path)
    assert _resolve_project_target(registry, str(tmp_path)) == str(entry.path)


def test_resolve_project_target_unknown_returns_none(tmp_path):
    registry = ProjectRegistry.single(tmp_path)
    assert _resolve_project_target(registry, "no-such-project") is None


def test_resolve_project_target_strips_quotes(tmp_path):
    registry = ProjectRegistry.single(tmp_path)
    entry = registry.default
    assert entry is not None
    quoted = '"' + str(tmp_path) + '"'
    assert _resolve_project_target(registry, quoted) == str(entry.path)
