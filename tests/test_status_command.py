"""/status and /effort are first-class, visible, and bilingual.

The command catalogs are the routing contract: a command that routes but is
missing from a catalog cannot drift into one language only, and the visible
menu must stay a subset of the catalog heads (the existing _commands filter
guarantees the rendering; these tests guarantee the data).
"""

from karox import tui as karox_tui


def test_status_and_effort_are_cataloged_in_both_languages():
    for name in ("/effort", "/status"):
        assert name in karox_tui.SLASH_COMMANDS
        assert name in karox_tui._COMMANDS_RU
        assert karox_tui.SLASH_COMMANDS[name].strip()
        assert karox_tui._COMMANDS_RU[name].strip()


def test_status_and_effort_are_visible_menu_entries():
    assert "/effort" in karox_tui.VISIBLE_COMMANDS
    assert "/status" in karox_tui.VISIBLE_COMMANDS
    for language in ("en", "ru"):
        visible = karox_tui._commands(language)
        heads = {name.split(" ", 1)[0] for name in visible}
        assert "/effort" in heads
        assert "/status" in heads


def test_every_visible_command_has_catalog_entries_in_both_languages():
    en_heads = {name.split(" ", 1)[0] for name in karox_tui.SLASH_COMMANDS}
    ru_heads = {name.split(" ", 1)[0] for name in karox_tui._COMMANDS_RU}
    for head in karox_tui.VISIBLE_COMMANDS:
        assert head in en_heads, f"{head} missing from SLASH_COMMANDS"
        assert head in ru_heads, f"{head} missing from _COMMANDS_RU"
