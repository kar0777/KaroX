"""browser.command dispatch and the typed browser error taxonomy.

The stable ``browser.command`` schema is the extension point for the guarded
action registry (mandate: schema fixed, new guarded actions inside). These
tests pin three contracts: every mandated action dispatches to its engine
method; an engine that lacks an action refuses with an engine-honest typed
message, never a generic unsupported-action error; and engine failures
classify into the one shared taxonomy instead of collapsing into a generic
browser failure.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from _support import SRC  # noqa: F401

from karox.browser_errors import (
    BROWSER_ERROR_KINDS,
    classify_browser_error,
    prefix_browser_error,
)
from karox.workspace_worker import InvalidCommand, execute_browser_command

_EXTENDED_ACTIONS = {
    "back": "back",
    "forward": "forward",
    "reload": "reload",
    "page_info": "page_info",
    "hover": "hover",
    "focus": "focus_element",
    "clear": "clear",
    "dblclick": "dblclick",
    "type": "type_text",
    "set_checked": "set_checked",
    "scroll": "scroll",
    "upload": "upload",
    "download": "download",
}


class _RecordingManager:
    """An engine stub that records which method served which action."""

    engine = "stub"
    takeover_active = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        for name in set(_EXTENDED_ACTIONS.values()):
            setattr(self, name, self._recorder(name))

    def _recorder(self, name: str):
        def method(payload: Any, deadline_seconds: float) -> dict[str, Any]:
            self.calls.append((name, dict(payload)))
            return {"result": "success", "method": name}

        return method


class _BareManager:
    """An engine with none of the extended surface."""

    engine = "bare"
    takeover_active = False


class _ExtensionRecordingManager:
    engine = "chrome_extension_mv3"
    takeover_active = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def _call(self, method: str, payload: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self.calls.append((method, dict(payload), deadline_seconds))
        return {"shown": True, "window_id": 7}


def _runtime(manager: Any) -> Any:
    return SimpleNamespace(_browser=manager)


class DispatchTests(unittest.TestCase):
    def test_every_extended_action_reaches_its_engine_method(self) -> None:
        for action, method_name in _EXTENDED_ACTIONS.items():
            with self.subTest(action=action):
                manager = _RecordingManager()
                result = execute_browser_command(
                    _runtime(manager),
                    {"action": action, "payload": {"selector": "#x"}},
                    5.0,
                )
                self.assertEqual(result["action"], action)
                self.assertEqual(result["result"], "success")
                self.assertEqual(manager.calls, [(method_name, {"selector": "#x"})])

    def test_show_window_routes_only_to_extension_protocol(self) -> None:
        manager = _ExtensionRecordingManager()
        result = execute_browser_command(
            _runtime(manager),
            {"action": "show_window", "payload": {"left": 120, "top": 80}},
            5.0,
        )
        self.assertTrue(result["shown"])
        self.assertEqual(manager.calls, [("show_window", {"left": 120, "top": 80}, 5.0)])

    def test_show_window_refuses_malformed_coordinates_with_a_typed_error(self) -> None:
        """Malformed coordinates are an invalid request, not an untyped crash.

        ``int()`` on a string, list, bool, None, or NaN used to escape as a bare
        ValueError/TypeError; every other browser action refuses with a typed
        InvalidCommand, and the engine must never see the malformed call.
        """
        for payload in (
            {"left": "120"},
            {"top": None},
            {"left": [120]},
            {"left": True},
            {"top": 12.5},
            {"left": float("nan")},
            {"left": 10_001},
        ):
            with self.subTest(payload=payload):
                manager = _ExtensionRecordingManager()
                with self.assertRaises(InvalidCommand):
                    execute_browser_command(
                        _runtime(manager),
                        {"action": "show_window", "payload": payload},
                        5.0,
                    )
                self.assertEqual(manager.calls, [])
        # A whole-number float is a valid coordinate and reaches the engine as int.
        manager = _ExtensionRecordingManager()
        execute_browser_command(
            _runtime(manager),
            {"action": "show_window", "payload": {"left": 120.0, "top": 80}},
            5.0,
        )
        self.assertEqual(manager.calls, [("show_window", {"left": 120, "top": 80}, 5.0)])

    def test_an_engine_without_the_action_refuses_honestly(self) -> None:
        for action in _EXTENDED_ACTIONS:
            with self.subTest(action=action):
                with self.assertRaises(InvalidCommand) as caught:
                    execute_browser_command(
                        _runtime(_BareManager()),
                        {"action": action, "payload": {}},
                        5.0,
                    )
                self.assertIn("not supported by this browser engine", str(caught.exception))

    def test_an_unknown_action_stays_a_distinct_error(self) -> None:
        with self.assertRaises(InvalidCommand) as caught:
            execute_browser_command(
                _runtime(_RecordingManager()),
                {"action": "teleport", "payload": {}},
                5.0,
            )
        self.assertIn("unsupported browser.command action", str(caught.exception))


class TaxonomyTests(unittest.TestCase):
    def test_every_engine_message_maps_to_its_kind(self) -> None:
        expectations = {
            "element not found": "element_not_found",
            "ambiguous target: 3 candidates": "ambiguous_target",
            "wait_for timed out": "navigation_timeout",
            "tab does not exist": "page_closed",
            "cannot close the last remaining tab": "page_closed",
            "extension is not connected": "browser_disconnected",
            "net::ERR_NAME_NOT_RESOLVED": "network_error",
            "download_failure: interrupted": "download_failure",
            "Failed to capture tab: image readback failed": "screenshot_capture_failed",
            "permission_denied: downloads permission is not granted": "permission_denied",
            "user takeover is active; agent click is paused": "user_takeover_required",
            "something nobody classified": "browser_error",
        }
        for message, kind in expectations.items():
            with self.subTest(message=message):
                self.assertEqual(classify_browser_error(message), kind)

    def test_prefixing_never_stacks_kinds(self) -> None:
        once = prefix_browser_error("element not found")
        self.assertEqual(once, "element_not_found: element not found")
        self.assertEqual(prefix_browser_error(once), once)

    def test_the_taxonomy_is_the_mandated_vocabulary(self) -> None:
        for kind in (
            "element_not_found",
            "ambiguous_target",
            "navigation_timeout",
            "page_closed",
            "browser_disconnected",
            "network_error",
            "download_failure",
            "screenshot_capture_failed",
            "permission_denied",
            "user_takeover_required",
        ):
            self.assertIn(kind, BROWSER_ERROR_KINDS)


if __name__ == "__main__":
    unittest.main()
