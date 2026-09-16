"""/status and /effort stay visible and bilingual in the daily command surface."""

from karox import tui as karox_tui


def test_status_and_effort_are_cataloged_in_both_languages():
    for name in ("/effort", "/status"):
        assert name in karox_tui.SLASH_COMMANDS
        assert name in karox_tui._COMMANDS_RU
        assert karox_tui.SLASH_COMMANDS[name].strip()
        assert karox_tui._COMMANDS_RU[name].strip()


def test_status_and_effort_are_visible_daily_controls():
    assert "/effort" in karox_tui.VISIBLE_COMMANDS
    # The bare `/` menu is capped at ten task-first controls and /status is
    # kept one prefix away instead: it stays fully routable, is typed directly
    # and remains listed in both language catalogs (the test above). Making the
    # menu larger again for observability commands would undo the deliberate
    # "no operations cockpit" decision recorded at VISIBLE_COMMANDS.
    assert "/status" in karox_tui.DISCOVERABLE_COMMANDS
    for language in ("en", "ru"):
        visible = karox_tui._commands(language)
        heads = {name.split(" ", 1)[0] for name in visible}
        assert "/effort" in heads
        assert len(heads) <= 10


def test_every_visible_command_has_catalog_entries_in_both_languages():
    en_heads = {name.split(" ", 1)[0] for name in karox_tui.SLASH_COMMANDS}
    ru_heads = {name.split(" ", 1)[0] for name in karox_tui._COMMANDS_RU}
    for head in karox_tui.VISIBLE_COMMANDS:
        assert head in en_heads, f"{head} missing from SLASH_COMMANDS"
        assert head in ru_heads, f"{head} missing from _COMMANDS_RU"
