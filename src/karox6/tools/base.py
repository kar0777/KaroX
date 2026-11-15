"""Tool contract, the context tools run in, and argument validation."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from karox6.core.events import Event
    from karox6.tools.files import FileTracker
    from karox6.tools.jobs import JobManager
    from karox6.tools.shell import ShellInfo


@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalRequest:
    tool: str
    summary: str  # what would run, e.g. the shell command
    rule: str  # matched rule id
    reason: str  # human explanation of the rule


Approver = Callable[[ApprovalRequest], Awaitable[bool]]


async def deny_all(_request: ApprovalRequest) -> bool:
    return False


@dataclass(slots=True)
class ToolContext:
    workspace: Path
    session_dir: Path
    files: FileTracker
    jobs: JobManager
    shell: ShellInfo
    approve: Approver = deny_all
    emit: Callable[[Event], None] = lambda _event: None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def spill_dir(self) -> Path:
        path = self.session_dir / "spill"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, raw: str) -> Path:
        """Resolve a model-given path against the workspace (absolute paths pass through)."""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return Path(_normpath(str(path)))


def _normpath(path: str) -> str:
    import os

    return os.path.normpath(path)


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool  # may run concurrently with other read-only calls

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class ToolArgsError(ValueError):
    pass


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def parse_arguments(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Parse model-produced JSON arguments and validate them against a flat schema.

    Lenient where the intent is unambiguous (``"120"`` for an integer, ``"true"``
    for a boolean, ``null`` for an optional field); strict otherwise, with one
    message listing every violation so the model can fix the call in one go.
    """
    text = raw.strip() or "{}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolArgsError(f"arguments are not valid JSON ({exc.msg} at char {exc.pos})") from exc
    if not isinstance(value, dict):
        raise ToolArgsError("arguments must be a JSON object")
    props: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    problems: list[str] = []
    out: dict[str, Any] = {}
    for key, val in value.items():
        spec = props.get(key)
        if spec is None:
            out[key] = val  # unknown keys pass through unvalidated (tools may accept aliases)
            continue
        if val is None:
            if key in required:
                problems.append(f"'{key}' must not be null")
            continue
        coerced, ok = _coerce(val, spec)
        if not ok:
            problems.append(f"'{key}' must be {spec.get('type')}, got {type(val).__name__}")
            continue
        if "enum" in spec and coerced not in spec["enum"]:
            problems.append(f"'{key}' must be one of {spec['enum']}")
            continue
        out[key] = coerced
    for key in required:
        if key not in out and not any(p.startswith(f"'{key}'") for p in problems):
            problems.append(f"missing required '{key}'")
    if problems:
        raise ToolArgsError("invalid arguments: " + "; ".join(problems))
    return out


def _coerce(val: Any, spec: dict[str, Any]) -> tuple[Any, bool]:
    kind = spec.get("type")
    if kind is None:
        return val, True
    expected = _JSON_TYPES.get(kind) if isinstance(kind, str) else None
    if expected is None:
        return val, True
    if kind in ("integer", "number") and isinstance(val, bool):
        return val, False
    if isinstance(val, expected):
        if kind == "integer" and isinstance(val, float):
            return (int(val), True) if val.is_integer() else (val, False)
        return val, True
    if kind == "integer" and isinstance(val, float) and val.is_integer():
        return int(val), True
    if isinstance(val, str):
        text = val.strip()
        if kind == "integer":
            try:
                return int(text), True
            except ValueError:
                return val, False
        if kind == "number":
            try:
                return float(text), True
            except ValueError:
                return val, False
        if kind == "boolean" and text.lower() in ("true", "false"):
            return text.lower() == "true", True
        if kind in ("array", "object"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return val, False
            return (parsed, True) if isinstance(parsed, expected) else (val, False)
    return val, False
