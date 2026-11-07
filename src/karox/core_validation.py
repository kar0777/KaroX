"""Core argument normalization and JSON-schema validation."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List
from .core_types import (
    InvalidCommand,
    ToolDefinition,
)
from .models import CoreCommand
from .core_support import (  # noqa: F401
    WRITE_MODE_CREATE as WRITE_MODE_CREATE,
    WRITE_MODE_APPEND as WRITE_MODE_APPEND,
    VerificationRule as VerificationRule,
    _ripgrep_executable as _ripgrep_executable,
)


class CoreValidationMixin:
    """Methods composed into CoreRuntime; see core.py."""

    def _repo_relative_alias(self, value: Any) -> Any:
        """An absolute path inside the repository, as a repository-relative one."""
        if not isinstance(value, str) or not value:
            return value
        candidate = value.replace("\\", "/")
        try:
            root = Path(self.repository).resolve().as_posix().rstrip("/")
        except Exception:
            return value
        if candidate == root:
            return "."
        if candidate.startswith(root + "/"):
            return candidate[len(root) + 1 :] or "."
        return value

    def _normalize_command(self, command: CoreCommand) -> CoreCommand:
        arguments = command.arguments
        if not isinstance(arguments, dict):
            return command
        normalized = self._normalize_arguments(command.name, dict(arguments))
        if normalized == arguments:
            return command
        from dataclasses import replace as _dc_replace

        self._audit(
            "core.command.arguments_normalized",
            {
                "session_id": command.session_id,
                "command": command.name,
                "from": sorted(arguments),
                "to": sorted(normalized),
            },
        )
        return _dc_replace(command, arguments=normalized)

    def _unwrap_argument_wrapper(
        self, name: str, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Flatten a call that wrapped its fields in ``payload``/``arguments``.

        The race logged models copying ``browser.command``'s shape into a
        desktop tool: every declared field arrived inside ``payload``, so the
        call was refused as unknown arguments even though the intended action
        was unambiguous. The repair is deliberately narrow: exactly one
        undeclared key, named ``payload`` or ``arguments``, holding a non-empty
        object whose every key the tool declares. The flattened call still goes
        through the same validation, policy and risk gates as a flat one.
        """
        definition = self._definitions.get(name)
        if definition is None or not isinstance(args, dict) or not args:
            return args
        schema = definition.input_schema
        if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
            return args
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return args
        unknown = set(args).difference(properties)
        if len(unknown) != 1:
            return args
        wrapper = next(iter(unknown))
        if wrapper not in {"payload", "arguments"}:
            return args
        inner = args[wrapper]
        if not isinstance(inner, dict) or not inner:
            return args
        if not set(inner).issubset(properties):
            return args
        merged = dict(inner)
        # A declared sibling (for example app_id beside the wrapper) wins: the
        # wrapper only supplies the fields the caller did not pass directly.
        merged.update({key: value for key, value in args.items() if key != wrapper})
        return merged

    def _normalize_arguments(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        args = self._unwrap_argument_wrapper(name, args)
        if name == "repo.search":
            if "query" not in args:
                for alias in self._SEARCH_QUERY_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["query"] = args.pop(alias)
                        break
            if "query" not in args and isinstance(args.get("pattern"), str):
                # ripgrep habit: the search text passed as "pattern". Only when it
                # cannot be a glob, so a real file filter is never reinterpreted.
                pattern = args["pattern"]
                if pattern and not (set(pattern) & self._GLOB_CHARS) and "/" not in pattern:
                    args["query"] = args.pop("pattern")
            if "pattern" not in args:
                for alias in self._SEARCH_GLOB_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["pattern"] = args.pop(alias)
                        break
            if "path" not in args:
                for alias in self._PATH_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["path"] = args.pop(alias)
                        break
            for key in ("pattern", "path"):
                if key in args:
                    args[key] = self._repo_relative_alias(args[key])
            return args
        if name == "repo.read_lines":
            if "path" not in args:
                for alias in self._PATH_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["path"] = args.pop(alias)
                        break
            if "start" not in args:
                for alias in ("start_line", "line", "from", "line_start"):
                    if alias in args:
                        args["start"] = args.pop(alias)
                        break
            end = None
            for alias in ("end", "end_line", "to", "line_end", "stop"):
                if alias in args:
                    end = args.pop(alias)
                    break
            if "count" not in args:
                for alias in ("limit", "lines", "num_lines", "n"):
                    if alias in args:
                        args["count"] = args.pop(alias)
                        break
            if end is not None and "count" not in args:
                start = args.get("start", 1)
                if (
                    isinstance(end, int)
                    and not isinstance(end, bool)
                    and isinstance(start, int)
                    and not isinstance(start, bool)
                    and end >= start >= 1
                ):
                    # Inclusive end line, as editors and sed ranges use it.
                    args["count"] = min(end - start + 1, 1_500)
                else:
                    args["end"] = end  # keep it so strict validation explains
            if "path" in args:
                args["path"] = self._repo_relative_alias(args["path"])
            return args
        if name == "repo.read_file":
            if "path" not in args:
                for alias in self._PATH_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["path"] = args.pop(alias)
                        break
            # A range on a whole-file read: the whole file still answers it, so
            # the range is dropped instead of refusing the read.
            if "path" in args:
                for key in ("start", "count", "end", "limit", "offset", "start_line", "end_line"):
                    args.pop(key, None)
                args["path"] = self._repo_relative_alias(args["path"])
            return args
        if name == "repo.edit_file" and isinstance(args.get("edits"), list):
            edits = []
            for item in args["edits"]:
                if isinstance(item, dict) and (
                    ("old_string" in item and "old" not in item)
                    or ("new_string" in item and "new" not in item)
                ):
                    item = dict(item)
                    if "old_string" in item and "old" not in item:
                        item["old"] = item.pop("old_string")
                    if "new_string" in item and "new" not in item:
                        item["new"] = item.pop("new_string")
                edits.append(item)
            args["edits"] = edits
            return args
        return args

    @staticmethod
    def _validate_arguments(
        definition: ToolDefinition, arguments: Dict[str, Any]
    ) -> None:
        """Enforce the one JSON-Schema subset every Core tool declares.

        Built-in tools and registered external tools (MCP, browser, desktop)
        are validated by the same code on purpose. The desktop surface shipped
        integer coordinates that the older built-in validator did not know, so
        every click, scroll and window-id attach was refused before its handler
        ran. One validator cannot drift from the schemas it reads.
        """
        if not isinstance(arguments, dict):
            raise InvalidCommand("Core command arguments must be an object")
        CoreValidationMixin._validate_external_schema(definition.input_schema, "arguments")
        CoreValidationMixin._validate_external_value(
            arguments, definition.input_schema, "arguments", definition.name
        )

    @staticmethod
    def _schema_type_names(schema: Dict[str, Any], label: str) -> List[str]:
        """The declared type names, validated; empty when no type is declared."""

        if "type" not in schema:
            return []
        declared = schema["type"]
        if isinstance(declared, str):
            names = [declared]
        elif (
            isinstance(declared, list)
            and declared
            and all(isinstance(item, str) for item in declared)
            and len(set(declared)) == len(declared)
        ):
            names = declared
        else:
            raise InvalidCommand(f"{label} schema type is malformed")
        supported = {
            "null", "boolean", "integer", "number", "string", "array", "object"
        }
        unsupported = set(names).difference(supported)
        if unsupported:
            raise InvalidCommand(
                f"{label} schema uses unsupported types: {sorted(unsupported)}"
            )
        return names

    @staticmethod
    def _validate_external_schema(schema: Any, label: str) -> None:
        """Check the declared subset, not just the call, before trusting it."""

        if not isinstance(schema, dict):
            raise InvalidCommand(f"{label} schema must be an object")
        CoreValidationMixin._schema_type_names(schema, label)
        if "properties" in schema:
            properties = schema["properties"]
            if not isinstance(properties, dict):
                raise InvalidCommand(f"{label} schema properties must be an object")
            for name, child in properties.items():
                if not isinstance(name, str):
                    raise InvalidCommand(
                        f"{label} schema property names must be strings"
                    )
                CoreValidationMixin._validate_external_schema(
                    child, f"{label}.properties[{name!r}]"
                )
        if "required" in schema:
            required = schema["required"]
            if (
                not isinstance(required, list)
                or not all(isinstance(item, str) for item in required)
                or len(set(required)) != len(required)
            ):
                raise InvalidCommand(
                    f"{label} schema required must contain unique strings"
                )
        if "items" in schema:
            CoreValidationMixin._validate_external_schema(schema["items"], f"{label}.items")
        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            if not isinstance(additional, bool):
                CoreValidationMixin._validate_external_schema(
                    additional, f"{label}.additionalProperties"
                )
        if "enum" in schema:
            options = schema["enum"]
            if not isinstance(options, list) or not options:
                raise InvalidCommand(
                    f"{label} schema enum must be a non-empty array"
                )
        for key in ("minimum", "maximum"):
            if key in schema:
                bound = schema[key]
                if (
                    isinstance(bound, bool)
                    or not isinstance(bound, (int, float))
                    or not math.isfinite(float(bound))
                ):
                    raise InvalidCommand(
                        f"{label} schema {key} must be a finite number"
                    )
        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            if key in schema:
                limit = schema[key]
                if (
                    isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or limit < 0
                ):
                    raise InvalidCommand(
                        f"{label} schema {key} must be a non-negative integer"
                    )
        if "pattern" in schema and not isinstance(schema["pattern"], str):
            raise InvalidCommand(f"{label} schema pattern must be a string")

    @staticmethod
    def _json_equal(left: Any, right: Any) -> bool:
        """JSON equality: ``1`` equals ``1.0`` but never equals ``true``."""

        if isinstance(left, bool) or isinstance(right, bool):
            return (
                isinstance(left, bool)
                and isinstance(right, bool)
                and left == right
            )
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if isinstance(left, dict) and isinstance(right, dict):
            return set(left) == set(right) and all(
                CoreValidationMixin._json_equal(left[key], right[key]) for key in left
            )
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(
                CoreValidationMixin._json_equal(a, b) for a, b in zip(left, right)
            )
        return type(left) is type(right) and left == right

    @staticmethod
    def _validate_constraints(value: Any, schema: Dict[str, Any], label: str) -> None:
        """Enforce the bounds the schema declares, on the value it describes."""

        options = schema.get("enum")
        if isinstance(options, list) and options:
            if not any(CoreValidationMixin._json_equal(value, option) for option in options):
                raise InvalidCommand(f"{label} must be one of {options}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            if (
                isinstance(minimum, (int, float))
                and not isinstance(minimum, bool)
                and value < minimum
            ):
                raise InvalidCommand(f"{label} must be at least {minimum}")
            maximum = schema.get("maximum")
            if (
                isinstance(maximum, (int, float))
                and not isinstance(maximum, bool)
                and value > maximum
            ):
                raise InvalidCommand(f"{label} must be at most {maximum}")
        if isinstance(value, str):
            min_length = schema.get("minLength")
            if (
                isinstance(min_length, int)
                and not isinstance(min_length, bool)
                and len(value) < min_length
            ):
                raise InvalidCommand(
                    f"{label} must be at least {min_length} characters"
                )
            max_length = schema.get("maxLength")
            if (
                isinstance(max_length, int)
                and not isinstance(max_length, bool)
                and len(value) > max_length
            ):
                raise InvalidCommand(
                    f"{label} must be at most {max_length} characters"
                )
            pattern = schema.get("pattern")
            if isinstance(pattern, str):
                try:
                    compiled = re.compile(pattern)
                except re.error:
                    # A remote schema may use ECMA-262 syntax Python rejects.
                    # The keyword is advisory there; the type and length checks
                    # still hold, so the call is not refused for a dialect gap.
                    compiled = None
                if compiled is not None and compiled.search(value) is None:
                    raise InvalidCommand(f"{label} does not match {pattern!r}")
        if isinstance(value, list):
            min_items = schema.get("minItems")
            if (
                isinstance(min_items, int)
                and not isinstance(min_items, bool)
                and len(value) < min_items
            ):
                raise InvalidCommand(f"{label} must contain at least {min_items} items")
            max_items = schema.get("maxItems")
            if (
                isinstance(max_items, int)
                and not isinstance(max_items, bool)
                and len(value) > max_items
            ):
                raise InvalidCommand(f"{label} must contain at most {max_items} items")

    @staticmethod
    def _validate_external_value(
        value: Any, schema: Any, label: str, tool_name: str = ""
    ) -> None:
        """Enforce the safe JSON-Schema subset understood by Core.

        Unknown keywords are intentionally ignored so a valid remote schema is
        not rejected merely because Core does not implement every draft
        feature. Declared constraints are enforced: a schema that promises an
        enum or a bound and does not hold it is documentation that lies.
        """

        if not isinstance(schema, dict):
            raise InvalidCommand(f"{label} schema must be an object")
        type_names = CoreValidationMixin._schema_type_names(schema, label)
        if type_names:
            if not any(
                CoreValidationMixin._external_type_matches(value, item) for item in type_names
            ):
                expected = " or ".join(type_names)
                raise InvalidCommand(f"{label} must be {expected}")
        CoreValidationMixin._validate_constraints(value, schema, label)
        if isinstance(value, dict):
            properties = schema.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            required = schema.get("required")
            if isinstance(required, list) and all(
                isinstance(item, str) for item in required
            ):
                missing = set(required).difference(value)
                if missing:
                    raise InvalidCommand(f"missing arguments: {sorted(missing)}")
            if schema.get("additionalProperties") is False:
                unknown = set(value).difference(properties)
                if unknown:
                    # Name the accepted arguments so the model can repair the
                    # call in one step instead of concluding the tool is
                    # unusable (the race logged exactly that failure mode).
                    hint = (
                        f"; {tool_name} accepts: {sorted(properties)}"
                        if tool_name
                        else ""
                    )
                    raise InvalidCommand(
                        f"unknown arguments: {sorted(unknown)}{hint}"
                    )
            additional = schema.get("additionalProperties")
            for name, item in value.items():
                child_schema = properties.get(name)
                if not isinstance(child_schema, dict) and isinstance(additional, dict):
                    child_schema = additional
                if isinstance(child_schema, dict):
                    CoreValidationMixin._validate_external_value(item, child_schema, name)
        elif isinstance(value, list):
            items = schema.get("items")
            if isinstance(items, dict):
                for index, item in enumerate(value):
                    CoreValidationMixin._validate_external_value(
                        item, items, f"{label}[{index}]"
                    )

    @staticmethod
    def _external_type_matches(value: Any, type_name: str) -> bool:
        if type_name == "null":
            return value is None
        if type_name == "boolean":
            return isinstance(value, bool)
        if type_name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if type_name == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            return not isinstance(value, float) or math.isfinite(value)
        kinds = {"string": str, "array": list, "object": dict}
        kind = kinds.get(type_name)
        # External schemas are an authorization boundary: an unknown declared
        # type must never turn a mixed-type declaration into an allow-all rule.
        return False if kind is None else isinstance(value, kind)
