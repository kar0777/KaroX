"""One command registry: menu, /help, and both languages cannot drift.

The owner saw OLD commands after a restart. Inside one runtime the slash
menu and /help both derive from a single catalog pair filtered by
VISIBLE_COMMANDS; these tests pin that contract so the in-source catalogs
cannot silently diverge (the cross-runtime case is covered by
karox.build_identity naming stale installed code).
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401

from karox import tui


def _heads(catalog: dict[str, str]) -> set[str]:
    return {name.split(" ", 1)[0] for name in catalog}


class CommandCatalogTests(unittest.TestCase):
    def test_english_and_russian_offer_the_same_commands(self) -> None:
        self.assertEqual(_heads(tui.SLASH_COMMANDS), _heads(tui._COMMANDS_RU))

    def test_every_visible_command_exists_in_both_catalogs(self) -> None:
        for head in tui.VISIBLE_COMMANDS:
            self.assertIn(head, _heads(tui.SLASH_COMMANDS), head)
            self.assertIn(head, _heads(tui._COMMANDS_RU), head)

    def test_menu_and_help_derive_from_one_source(self) -> None:
        en = {name.split(" ", 1)[0] for name in tui._commands("en")}
        ru = {name.split(" ", 1)[0] for name in tui._commands("ru")}
        self.assertEqual(en, ru)
        self.assertEqual(en, set(tui.VISIBLE_COMMANDS) & _heads(tui.SLASH_COMMANDS))

    def test_no_description_is_blank(self) -> None:
        for catalog in (tui.SLASH_COMMANDS, tui._COMMANDS_RU):
            for name, description in catalog.items():
                self.assertTrue(description.strip(), name)

    def test_retired_aliases_stay_out_of_the_menu(self) -> None:
        for alias in tui.DEPRECATED_COMMAND_ALIASES:
            self.assertNotIn(alias, tui.VISIBLE_COMMANDS)


if __name__ == "__main__":
    unittest.main()
