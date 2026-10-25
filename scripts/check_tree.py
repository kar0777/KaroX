"""Deterministic hygiene gate for the release tree.

It answers four questions a code review should not have to:

* did a legacy module return;
* does every intra-package import resolve inside this package;
* did a generated cache get copied into package data;
* is the release still the size it claims to be.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "karox"
LEGACY = {
    "hosted_bridge.py",
    "hosted_tools_runtime.py",
    "autonomy_runtime.py",
    "autonomy_benchmark.py",
    "web_bridge_launcher.py",
    "tui.py",
    "tui_dashboard.py",
    "tui_connections.py",
    "tui_workspace.py",
    "tui_onboarding.py",
    "browser_access.py",
    "browser_session.py",
    "ellipsis_runtime.py",
    "ellipsis_api.py",
    "tailscale.py",
    "orchestrator.py",
}
# The release carries the hosted MCP bridge as a product surface. The v5
# laboratory was ~166k lines. 70k covered the release kernel; the seed race
# commit vendored the karo-core engine (~5.6k lines, src/karox/engine) and B1
# added its typed-tool adapter (engine_kernel.py), which is deliberate product
# surface, so the ceiling moved to 80k. B4 (browser engine, ~6.5k) and B5
# (computer use, ~6.2k) are the same kind of deliberate merged surface, so the
# merged integration moved it once more to 90k. Raise it deliberately for
# named surface, never to make a stray copy pass.
MAX_LINES = 90_000


def module_exists(module: str) -> bool:
    parts = module.split(".")
    candidate = PACKAGE.joinpath(*parts)
    if (candidate / "__init__.py").is_file():
        return True
    return PACKAGE.joinpath(*parts[:-1], parts[-1] + ".py").is_file()


def internal_imports() -> list[str]:
    missing: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        module = path.relative_to(PACKAGE).with_suffix("")
        parts = list(module.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        package = (
            ["karox", *parts]
            if path.name == "__init__.py"
            else ["karox", *parts[:-1]]
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = package[: len(package) - node.level + 1]
                if node.module is None:
                    targets = [
                        ".".join([*base, alias.name])
                        for alias in node.names
                        if not alias.name.startswith("_")
                    ]
                else:
                    targets = [".".join([*base, node.module])]
            elif node.module and node.module.startswith("karox."):
                targets = [node.module]
            else:
                continue
            for target in targets:
                short = target.removeprefix("karox.")
                if not module_exists(short):
                    missing.append(f"{module} -> {short}")
    return missing


def main() -> int:
    problems: list[str] = []
    present = {path.name for path in PACKAGE.iterdir() if path.is_file()}
    for name in sorted(present & LEGACY):
        problems.append(f"legacy module returned: {name}")
    for name in ("prompts", "builtin_skills", "skill_notes"):
        for path in (PACKAGE / name).rglob("*.pyc"):
            if not path.name.startswith("__init__."):
                problems.append(f"generated cache in package data: {path}")
    if "hosted_bridge" in (PACKAGE / "mcp_client.py").read_text(encoding="utf-8"):
        problems.append("mcp_client still imports the hosted bridge")
    problems.extend(f"unresolved import: {item}" for item in internal_imports())

    lines = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in PACKAGE.rglob("*.py")
    )
    if lines >= MAX_LINES:
        problems.append(f"package grew to {lines} lines (ceiling {MAX_LINES})")

    if problems:
        for problem in problems:
            print(f"check_tree: {problem}", file=sys.stderr)
        return 1
    modules = sum(1 for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)
    print(f"check_tree: ok · {modules} modules · {lines} lines · no legacy surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())