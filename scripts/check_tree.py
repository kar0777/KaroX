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
    "cache_scheduler.py",
    "cost_intelligence.py",
    "semantic_shadow.py",
}
#: The vendored karo-core engine is pruned to exactly the modules the product
#: imports. Adding a file under engine/ must be a deliberate act that updates
#: this set.
ENGINE_MODULES = {
    "__init__.py",
    "agents/__init__.py",
    "agents/default.py",
    "environments/__init__.py",
    "environments/local.py",
    "exceptions.py",
    "utils/__init__.py",
    "utils/log.py",
    "utils/serialize.py",
}
# The retired v5 loop and its economy modules were removed (~8.2k lines) and
# the vendored engine was pruned to the product's import closure (~3.6k
# lines), so the ceiling dropped from 90k to 80k. The test raised it four times
# afterwards (83k in 108fc1d, 85k in 193e247, 86k in 630be7c, 87k in 44c46f1)
# while this script kept 80k, which is how the two release gates disagreed.
# The budget is declared once, here: tests/test_tree.py reads this module
# instead of carrying a second number.
#
# 88k (2026-11-06): the package measured 86,996 lines. Since the 87k ceiling
# the deliberate Missions 01-05 added 622 net source lines (verification reuse,
# the whole-change review, the durable run baseline and its Git-ignored
# coverage), plus the 36-line closing-page change in ae9f96b; no generated or
# duplicated module was found. That left 1,004 lines (~1.2%) of headroom,
# several ordinary increments rather than a jump to 100k. A stray copy of the
# package or a generated dump is thousands of lines and still fails here.
#
# 91.5k (2026-11-08, third raise): measured 90,643 lines after decomposing the
# bridge launcher/proxy/tailscale on top of the earlier engine, tools, providers,
# MCP, hosted, disk, TUI and web-check splits. The delta over the 87,689 baseline
# is owner-module docstrings/imports (per-batch before/after is in docs/deep/03),
# not duplicated logic. The remaining bridge profiles/oauth and route slices are
# projected to need at most ~500 lines of that structural overhead. Any further
# raise requires a fresh measurement and evidence that no behavior was added.
MAX_PACKAGE_LINES = 96_500  # raised 2026-11-12 by the owner: improvements first, refactor later (96k: code mode, 2026-11-14; 96.5k: v60.4 tool-API leniency, 2026-11-15)


def package_line_count(package: Path) -> int:
    """Count every ``*.py`` line under *package* exactly as both gates did."""
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in package.rglob("*.py")
    )


def line_budget_problem(
    lines: int, ceiling: int = MAX_PACKAGE_LINES
) -> str | None:
    """The single line-budget decision: ``count >= ceiling`` fails closed."""
    if lines >= ceiling:
        return f"package grew to {lines} lines (ceiling {ceiling})"
    return None


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
    engine = PACKAGE / "engine"
    engine_present = {
        str(path.relative_to(engine)).replace("\\", "/")
        for path in engine.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    for name in sorted(engine_present - ENGINE_MODULES):
        problems.append(f"engine module outside the vendored closure: {name}")
    for name in ("prompts", "builtin_skills"):
        for path in (PACKAGE / name).rglob("*.pyc"):
            if not path.name.startswith("__init__."):
                problems.append(f"generated cache in package data: {path}")
    if "hosted_bridge" in (PACKAGE / "mcp_client.py").read_text(encoding="utf-8"):
        problems.append("mcp_client still imports the hosted bridge")
    problems.extend(f"unresolved import: {item}" for item in internal_imports())

    lines = package_line_count(PACKAGE)
    problem = line_budget_problem(lines)
    if problem is not None:
        problems.append(problem)

    if problems:
        for problem in problems:
            print(f"check_tree: {problem}", file=sys.stderr)
        return 1
    modules = sum(1 for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)
    print(f"check_tree: ok · {modules} modules · {lines} lines · no legacy surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())