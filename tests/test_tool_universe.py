"""Deferred tool universe: deterministic family selection and safe discovery.

These tests pin the module contract: selection is a pure function of the
task text, families keep one fixed order, the coding lane can never be
deselected, and the discovery note stays bounded and names-only. The kernel
wiring is proven separately in test_tool_universe_wiring.py.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from karox.tool_universe import (
    ALWAYS_FAMILIES,
    FAMILY_ORDER,
    UniverseSelection,
    discovery_note,
    family_of,
    select_families,
)


class FamilyOfTests(unittest.TestCase):
    def test_kernel_space_names_resolve(self) -> None:
        self.assertEqual(family_of("repo.read_file"), "core")
        self.assertEqual(family_of("git.status"), "core")
        self.assertEqual(family_of("tests.run"), "core")
        self.assertEqual(family_of("checks.run"), "core")
        self.assertEqual(family_of("browser.fetch"), "browser")
        self.assertEqual(family_of("dev.command"), "process")
        self.assertEqual(family_of("process.start"), "process")
        self.assertEqual(family_of("command.run"), "process")
        self.assertEqual(family_of("runtime.status"), "admin")
        self.assertEqual(family_of("report.get"), "admin")
        self.assertEqual(family_of("memory.recall"), "memory")
        self.assertEqual(family_of("task.checkpoint"), "task")

    def test_hosted_names_resolve_identically(self) -> None:
        self.assertEqual(family_of("karox.browser.open"), "browser")
        self.assertEqual(family_of("karox.runtime.restart"), "admin")
        self.assertEqual(family_of("karox.repo.search"), "core")
        self.assertEqual(family_of("karox.dev_server.status"), "process")

    def test_unknown_prefix_degrades_to_always_advertised(self) -> None:
        self.assertEqual(family_of("totally.new_tool"), "core")
        self.assertIn("core", ALWAYS_FAMILIES)


class SelectFamiliesTests(unittest.TestCase):
    def test_neutral_task_keeps_only_always_families(self) -> None:
        selection = select_families("what does sample.txt contain?")
        self.assertEqual(selection.families, ALWAYS_FAMILIES)
        self.assertEqual(selection.omitted, ("disk", "browser", "process", "admin"))
        self.assertEqual(selection.reasons, ())

    def test_selection_is_deterministic(self) -> None:
        first = select_families("fix the failing unit test")
        second = select_families("fix the failing unit test")
        self.assertEqual(first, second)
        self.assertIsInstance(first, UniverseSelection)

    def test_families_follow_fixed_order(self) -> None:
        text = "restart the server, open the browser and check diagnostics"
        selection = select_families(text)
        order = [FAMILY_ORDER.index(name) for name in selection.families]
        self.assertEqual(order, sorted(order))
        self.assertEqual(
            selection.families,
            ("core", "task", "memory", "browser", "process", "admin"),
        )
        self.assertEqual(selection.omitted, ("disk",))

    def test_english_browser_signal(self) -> None:
        selection = select_families("Take a screenshot of the page")
        self.assertIn("browser", selection.families)
        self.assertTrue(
            any(reason.startswith("browser:") for reason in selection.reasons)
        )

    def test_russian_signals(self) -> None:
        browser = select_families("Открой сайт и сделай скриншот")
        self.assertIn("browser", browser.families)
        process = select_families("перезапусти сервер на порту 4173")
        self.assertIn("process", process.families)
        admin = select_families("проверь диагностику рантайма")
        self.assertIn("admin", admin.families)

    def test_url_is_a_browser_signal(self) -> None:
        selection = select_families("verify https://example.test loads")
        self.assertIn("browser", selection.families)

    def test_word_boundaries_do_not_overmatch(self) -> None:
        # "format" must not trigger the browser family via "form", and
        # "transportable" must not trigger the process family via "port".
        selection = select_families("format the transportable data nicely")
        self.assertEqual(selection.families, ALWAYS_FAMILIES)

    def test_non_string_input_degrades_to_always(self) -> None:
        selection = select_families(None)  # type: ignore[arg-type]
        self.assertEqual(selection.families, ALWAYS_FAMILIES)


class DiscoveryNoteTests(unittest.TestCase):
    def test_empty_mapping_renders_nothing(self) -> None:
        self.assertEqual(discovery_note({}), "")
        self.assertEqual(discovery_note({"browser": []}), "")

    def test_names_are_sorted_and_grouped_in_family_order(self) -> None:
        note = discovery_note(
            {
                "process": ["process_stop", "dev_command"],
                "browser": ["browser_fetch", "browser_actions"],
            }
        )
        self.assertIn("browser[browser_actions,browser_fetch]", note)
        self.assertIn("process[dev_command,process_stop]", note)
        self.assertLess(note.index("browser["), note.index("process["))
        self.assertIn("exact name", note)

    def test_note_is_names_only_and_bounded(self) -> None:
        many = {"browser": [f"browser_tool_{i:03d}" for i in range(200)]}
        note = discovery_note(many)
        self.assertLessEqual(len(note), 1200)
        self.assertNotIn("{", note)
        self.assertNotIn("parameters", note)

    def test_note_is_deterministic(self) -> None:
        mapping = {"admin": ["runtime_status"], "browser": ["browser_fetch"]}
        self.assertEqual(discovery_note(mapping), discovery_note(mapping))


if __name__ == "__main__":
    unittest.main()
