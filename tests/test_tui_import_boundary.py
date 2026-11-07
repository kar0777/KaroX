"""The terminal client must stay a client.

The TUI may use pure presentation/platform helpers and its own package, but it
must not import the agent runtime, core execution, engine or session store. That
boundary is what lets the client run against a recorded JSON stream.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TUI = ROOT / "src" / "karox" / "tui"
FORBIDDEN = (
    "karox.agent",
    "karox.core",
    "karox.engine",
    "karox.engine_kernel",
    "karox.engine_tool_env",
    "karox.action_policy",
    "karox.completion_gate",
    "karox.sessions",
    "karox.policy",
)


def _absolute(module: str | None, level: int, name: str = "") -> str:
    if level == 0:
        return module or name
    # Files in karox.tui: level 1 = karox.tui, level 2 = karox.
    base = ["karox", "tui"][: max(0, 2 - level + 1)]
    parts = base + ([module] if module else []) + ([name] if name else [])
    return ".".join(part for part in parts if part)


def test_tui_does_not_import_the_agent_runtime() -> None:
    violations: list[str] = []
    for path in sorted(TUI.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [_absolute(node.module, node.level)]
                targets.extend(
                    _absolute(node.module, node.level, alias.name)
                    for alias in node.names
                    if alias.name != "*"
                )
            for target in targets:
                if any(target == item or target.startswith(item + ".") for item in FORBIDDEN):
                    violations.append(f"{path.name}:{node.lineno}: {target}")
    assert not violations, "\n".join(violations)
