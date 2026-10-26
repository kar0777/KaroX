"""The one argument contract every Core tool call passes through.

Built-in tools and registered external tools (MCP, browser, desktop) declare
the same bounded JSON-Schema subset. These tests pin the subset itself and the
failure that motivated the unification: the desktop surface's integer
arguments were rejected before their handlers ran, so clicks, scrolls and
window-id attaches could never work.
"""

from __future__ import annotations

import math

import pytest

from karox.core import CoreRuntime
from karox.core_types import ToolDefinition
from karox.models import Capability


def _definition(schema: dict, name: str = "probe.tool") -> ToolDefinition:
    return ToolDefinition(name, "probe", Capability.DESKTOP_INPUT, False, schema)


def _valid(schema: dict, arguments: dict) -> None:
    CoreRuntime._validate_arguments(_definition(schema), arguments)


def _invalid(schema: dict, arguments: dict, message: str) -> None:
    with pytest.raises(Exception) as excinfo:
        CoreRuntime._validate_arguments(_definition(schema), arguments)
    assert message in str(excinfo.value)


def test_integer_arguments_are_accepted_for_builtin_style_tools() -> None:
    """The race failure: every desktop coordinate was refused as a schema type.

    ``desktop.click``/``scroll``/``attach`` declare ``integer`` arguments and
    are registered as built-in tools, so the older built-in validator's type
    table (which lacked integer) refused them all before the handler ran.
    """

    schema = {
        "type": "object",
        "properties": {
            "app_id": {"type": "string"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "clicks": {"type": "integer", "minimum": 1, "maximum": 4},
        },
        "required": ["app_id", "x", "y"],
        "additionalProperties": False,
    }
    _valid(schema, {"app_id": "editor", "x": 10, "y": 20})
    _valid(schema, {"app_id": "editor", "x": 10, "y": 20, "clicks": 2})
    _invalid(schema, {"app_id": "editor", "x": True, "y": 20}, "x must be integer")
    _invalid(schema, {"app_id": "editor", "x": 10}, "missing arguments: ['y']")
    _invalid(
        schema,
        {"app_id": "editor", "x": 10, "y": 20, "clicks": 5},
        "clicks must be at most 4",
    )


def test_number_rejects_bool_and_non_finite() -> None:
    schema = {"type": "object", "properties": {"timeout_seconds": {"type": "number"}}}
    _valid(schema, {"timeout_seconds": 2})
    _valid(schema, {"timeout_seconds": 2.5})
    _invalid(schema, {"timeout_seconds": True}, "timeout_seconds must be number")
    _invalid(
        schema,
        {"timeout_seconds": math.inf},
        "timeout_seconds must be number",
    )


def test_enum_is_enforced_and_json_semantics_keep_bool_distinct() -> None:
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["observe", "control"]},
            "flag": {"type": "boolean", "enum": [True]},
        },
    }
    _valid(schema, {"mode": "control"})
    _invalid(schema, {"mode": "root"}, "mode must be one of")
    _valid(schema, {"flag": True})
    _invalid(schema, {"flag": False}, "flag must be one of")


def test_string_length_and_pattern_are_enforced() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 3,
                "maxLength": 6,
                "pattern": "^[a-z]+$",
            }
        },
    }
    _valid(schema, {"name": "abc"})
    _invalid(schema, {"name": "ab"}, "name must be at least 3 characters")
    _invalid(schema, {"name": "abcdefg"}, "name must be at most 6 characters")
    _invalid(schema, {"name": "ABC"}, "name does not match")


def test_array_bounds_and_nested_items_are_enforced() -> None:
    schema = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {"old": {"type": "string"}},
                    "required": ["old"],
                    "additionalProperties": False,
                },
            }
        },
    }
    _valid(schema, {"edits": [{"old": "a"}, {"old": "b"}]})
    _invalid(schema, {"edits": []}, "edits must contain at least 1 items")
    _invalid(
        schema,
        {"edits": [{"old": "a"}, {"old": "b"}, {"old": "c"}]},
        "edits must contain at most 2 items",
    )
    _invalid(schema, {"edits": [{"new": "b"}]}, "missing arguments: ['old']")
    _invalid(schema, {"edits": [{"old": "a", "extra": 1}]}, "unknown arguments")


def test_union_types_accept_both_declared_shapes() -> None:
    schema = {
        "type": "object",
        "properties": {"occurrence": {"type": ["string", "integer"]}},
    }
    _valid(schema, {"occurrence": "all"})
    _valid(schema, {"occurrence": 2})
    _invalid(schema, {"occurrence": True}, "occurrence must be string or integer")


def test_unknown_arguments_name_what_the_tool_accepts() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
        "additionalProperties": False,
    }
    with pytest.raises(Exception) as excinfo:
        CoreRuntime._validate_arguments(
            _definition(schema, "desktop.click"),
            {"payload": {"x": 1, "y": 2}},
        )
    message = str(excinfo.value)
    assert "unknown arguments: ['payload']" in message
    assert "desktop.click accepts: ['x', 'y']" in message


def test_malformed_schema_constraints_are_refused_before_the_call() -> None:
    for schema, message in [
        ({"type": "object", "properties": {}, "enum": []}, "enum must be a non-empty"),
        (
            {"type": "object", "properties": {"n": {"type": "integer", "minimum": True}}},
            "minimum must be a finite number",
        ),
        (
            {"type": "object", "properties": {"n": {"type": "string", "minLength": -1}}},
            "minLength must be a non-negative integer",
        ),
        (
            {"type": "object", "properties": {"n": {"type": "string", "pattern": 7}}},
            "pattern must be a string",
        ),
        (
            {"type": "object", "properties": {"n": {"type": "wibble"}}},
            "unsupported types",
        ),
    ]:
        with pytest.raises(Exception) as excinfo:
            CoreRuntime._validate_arguments(_definition(schema), {})
        assert message in str(excinfo.value)


def test_the_full_desktop_surface_validates_its_own_declared_types() -> None:
    """Every desktop tool accepts a legal call built from its own schema.

    This is the regression guard for the race: checking the declaration is not
    enough, the validator must accept the value shapes those declarations
    promise (integer coordinates, enum access, bounded limits, composite
    message mode).
    """

    from karox.computer_use.adapter import TOOL_SPECS

    samples = {
        "desktop.status": {},
        "desktop.list_windows": {"limit": 10},
        "desktop.list_apps": {"limit": 10},
        "desktop.request_approval": {"action": "desktop.click", "payload": {}},
        "desktop.attach": {
            "app_id": "editor",
            "window_id": 1001,
            "access": "control",
        },
        "desktop.detach": {"app_id": "editor"},
        "desktop.capture": {"app_id": "editor", "max_dim": 1200},
        "desktop.inspect_ui": {"app_id": "editor", "max_nodes": 200},
        "desktop.type_text": {"app_id": "editor", "text": "hello", "pace_ms": 5},
        "desktop.press_key": {"app_id": "editor", "key": "ctrl+s"},
        "desktop.click": {
            "app_id": "editor",
            "x": 10,
            "y": 20,
            "button": "right",
            "clicks": 2,
        },
        "desktop.scroll": {"app_id": "editor", "x": 10, "y": 20, "clicks": -3},
        "desktop.launch_app": {"shortcut_path": "C:\\Start\\Notepad.lnk"},
        "desktop.show_window": {"app_id": "editor", "mode": "restore_noactivate"},
        "desktop.open_terminal": {"shell": "powershell", "keep_open": True},
    }
    assert set(samples) == set(TOOL_SPECS)
    for name, arguments in samples.items():
        spec = TOOL_SPECS[name]
        CoreRuntime._validate_arguments(
            ToolDefinition(
                name,
                str(spec["description"]),
                Capability.DESKTOP_INPUT,
                False,
                spec["input_schema"],
            ),
            arguments,
        )

def test_a_wrapped_call_is_flattened_only_when_the_shape_is_unambiguous(tmp_path) -> None:
    """Models copied browser.command's payload shape into a flat tool.

    One undeclared ``payload``/``arguments`` object whose keys are all declared
    is the same call in another shape, so it is flattened and still validated.
    Anything less clear stays refused with the accepted-argument hint.
    """

    from karox.models import AccessProfile
    from karox.policy import CapabilityPolicy
    from karox.sessions import SessionStore

    repository = tmp_path / "repo"
    repository.mkdir()
    core = CoreRuntime(
        repository,
        CapabilityPolicy(AccessProfile.ELEVATED),
        SessionStore(tmp_path / "sessions"),
        tmp_path / "audit.jsonl",
    )
    flat = {
        "type": "object",
        "properties": {
            "app_id": {"type": "string"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        },
        "required": ["app_id", "x", "y"],
        "additionalProperties": False,
    }
    core.register_tool(
        ToolDefinition("desktop.click", "probe", Capability.DESKTOP_INPUT, False, flat),
        lambda arguments, deadline: {"ok": True},
    )
    assert core._normalize_arguments(
        "desktop.click", {"payload": {"app_id": "editor", "x": 1, "y": 2}}
    ) == {"app_id": "editor", "x": 1, "y": 2}
    # A declared sibling wins over the wrapper copy.
    assert core._normalize_arguments(
        "desktop.click",
        {"app_id": "editor", "payload": {"app_id": "other", "x": 1, "y": 2}},
    ) == {"app_id": "editor", "x": 1, "y": 2}
    # An undeclared inner key means the shape is not unambiguous: refuse.
    assert core._normalize_arguments(
        "desktop.click", {"payload": {"mystery": 1}}
    ) == {"payload": {"mystery": 1}}
    # A tool that declares ``payload`` keeps it (browser.command, repo.command).
    declared = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "payload": {"type": "object"}},
        "required": ["action", "payload"],
        "additionalProperties": False,
    }
    core.register_tool(
        ToolDefinition("browser.command", "probe", Capability.DESKTOP_INPUT, False, declared),
        lambda arguments, deadline: {"ok": True},
    )
    original = {"action": "open", "payload": {"url": "http://127.0.0.1/"}}
    assert core._normalize_arguments("browser.command", dict(original)) == original
