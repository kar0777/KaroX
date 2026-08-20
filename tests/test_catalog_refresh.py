"""Catalog refresh and stale-client handling on the proxy wire.

The proxy serves stateless JSON responses (StreamableHTTPSessionManager with
``stateless=True``), so the MCP ``notifications/tools/list_changed`` push can
never reach a connected client and ``capabilities.tools.listChanged`` is
honestly ``false``. After a catalog upgrade (for example the karox.memory.*
tools arriving in schema snapshot v3), a client that cached the previous
``tools/list`` keeps calling stale names until it reconnects.

These are the regressions for that upgrade path: the wire must answer with an
actionable "reconnect this client" hint, must never propose destructive
recovery (new bridge, credential rotation, URL change), and both spellings of
every new tool name must resolve.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.proxy_server import (
    bridge_error_result,
    resolve_wire_tool_name,
    stale_catalog_hint,
    wire_tool_name,
)

_MEMORY_TOOLS = (
    "karox.memory.remember",
    "karox.memory.recall",
    "karox.memory.context",
    "karox.memory.list",
    "karox.memory.forget",
)

_OLD_CATALOG = tuple(
    f"karox_repo_{name}" for name in ("read_file", "search", "list_files")
)
_NEW_CATALOG = _OLD_CATALOG + tuple(wire_tool_name(name) for name in _MEMORY_TOOLS)


class StaleCatalogHintTests(unittest.TestCase):
    def test_counts_new_tools_when_cached_catalog_is_known(self) -> None:
        hint = stale_catalog_hint(
            "karox_memory_remember", _NEW_CATALOG, cached_wire_names=_OLD_CATALOG
        )
        self.assertIn("5 new tools are available", hint)
        self.assertIn("reconnect this client", hint)

    def test_single_new_tool_uses_singular_wording(self) -> None:
        hint = stale_catalog_hint(
            "karox_memory_recall",
            _OLD_CATALOG + (wire_tool_name("karox.memory.recall"),),
            cached_wire_names=_OLD_CATALOG,
        )
        self.assertIn("1 new tool is available", hint)

    def test_unknown_cache_still_gives_actionable_reconnect_hint(self) -> None:
        hint = stale_catalog_hint("karox_memory_remember", _NEW_CATALOG)
        self.assertIn("reconnect", hint)
        self.assertIn(str(len(set(_NEW_CATALOG))), hint)

    def test_hint_never_proposes_destructive_recovery(self) -> None:
        for cached in (None, _OLD_CATALOG, _NEW_CATALOG):
            hint = stale_catalog_hint(
                "karox_memory_remember", _NEW_CATALOG, cached_wire_names=cached
            )
            lowered = hint.lower()
            self.assertNotIn("new bridge", lowered)
            self.assertNotIn("rotate", lowered)
            self.assertNotIn("delete", lowered)
            self.assertIn("same bridge, same credential, same url", lowered)

    def test_identical_catalogs_fall_back_to_generic_hint(self) -> None:
        hint = stale_catalog_hint(
            "karox_typo_tool", _NEW_CATALOG, cached_wire_names=_NEW_CATALOG
        )
        self.assertIn("reconnect", hint)


class ToolNotExposedWireShapeTests(unittest.TestCase):
    def test_detail_extends_the_fixed_message(self) -> None:
        result = bridge_error_result(
            "tool_not_exposed",
            detail=stale_catalog_hint("karox_memory_remember", _NEW_CATALOG),
        )
        self.assertTrue(result.isError)
        structured = result.structuredContent or {}
        self.assertEqual(structured.get("error_code"), "tool_not_exposed")
        self.assertIn("reconnect", structured.get("error", ""))
        text = result.content[0].text
        self.assertTrue(text.startswith("tool_not_exposed:"))
        self.assertIn("reconnect", text)

    def test_error_without_detail_is_unchanged(self) -> None:
        result = bridge_error_result("tool_not_exposed")
        structured = result.structuredContent or {}
        self.assertEqual(
            structured.get("error"), "the tool is not exposed by this bridge"
        )


class CatalogUpgradeResolutionTests(unittest.TestCase):
    def test_every_new_memory_tool_resolves_in_both_spellings(self) -> None:
        for internal in _MEMORY_TOOLS:
            with self.subTest(tool=internal):
                self.assertEqual(
                    resolve_wire_tool_name(internal, _MEMORY_TOOLS), internal
                )
                self.assertEqual(
                    resolve_wire_tool_name(wire_tool_name(internal), _MEMORY_TOOLS),
                    internal,
                )

    def test_schema_snapshot_version_still_advertised_as_v3(self) -> None:
        from karox.client_capabilities import negotiate_client_capabilities

        snapshot = negotiate_client_capabilities(
            client_kind="chatgpt-web",
            available_tools=_MEMORY_TOOLS,
            access_profile="elevated",
            disabled_tools=[],
            practical_output_size_limit=4 * 1024 * 1024,
            persistent_session=True,
        )
        self.assertEqual(snapshot.tool_schema_snapshot_version, 3)


if __name__ == "__main__":
    unittest.main()
