"""P1.5 deterministic tool-catalog groups: no hidden magic per profile."""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.tool_catalog import GROUP_NAMES, catalog_groups, tool_group

_CATALOG = (
    "karox.repo.read_file",
    "karox.repo.write_file",
    "karox.git.status",
    "karox.tests.run",
    "karox.checks.start",
    "karox.artifact.get",
    "karox.task.bootstrap",
    "karox.task.checkpoint",
    "karox.memory.remember",
    "karox.memory.recall",
    "karox.browser.open",
    "karox.browser.click",
    "karox.dev_server.status",
    "karox.runtime.restart",
    "karox.bridge.diagnostics",
)


class ToolGroupTests(unittest.TestCase):
    def test_every_tool_lands_in_exactly_one_group(self) -> None:
        groups = catalog_groups(_CATALOG)
        flattened = [name for members in groups.values() for name in members]
        self.assertEqual(sorted(flattened), sorted(_CATALOG))
        self.assertEqual(len(flattened), len(set(flattened)))

    def test_group_assignment_is_the_documented_one(self) -> None:
        expectations = {
            "karox.repo.read_file": "core",
            "karox.git.status": "core",
            "karox.tests.run": "core",
            "karox.checks.start": "core",
            "karox.artifact.get": "core",
            "karox.task.bootstrap": "task",
            "karox.memory.remember": "memory",
            "karox.browser.open": "browser",
            "karox.dev_server.status": "devserver",
            "karox.runtime.restart": "admin",
            "karox.bridge.diagnostics": "admin",
        }
        for tool, group in expectations.items():
            with self.subTest(tool=tool):
                self.assertEqual(tool_group(tool), group)

    def test_unknown_prefix_falls_back_to_core_instead_of_raising(self) -> None:
        self.assertEqual(tool_group("karox.future.shiny"), "core")

    def test_groups_follow_fixed_order_and_sorted_members(self) -> None:
        groups = catalog_groups(_CATALOG)
        self.assertEqual(
            list(groups.keys()),
            [name for name in GROUP_NAMES if name in groups],
        )
        for members in groups.values():
            self.assertEqual(list(members), sorted(members))

    def test_equal_catalogs_produce_identical_payloads(self) -> None:
        first = catalog_groups(_CATALOG)
        second = catalog_groups(tuple(reversed(_CATALOG)))
        self.assertEqual(first, second)

    def test_empty_groups_are_omitted(self) -> None:
        groups = catalog_groups(("karox.repo.read_file",))
        self.assertEqual(list(groups.keys()), ["core"])


if __name__ == "__main__":
    unittest.main()
