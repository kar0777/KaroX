"""The release tree is a contract, not whatever happened to be copied.

These tests are the cheap, deterministic half of release hygiene: no legacy
module can quietly reappear, no cache can be committed, and every intra-package
import must resolve inside the release.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "karox"
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


def _load_release_gate() -> ModuleType:
    """Load the release script itself, so both gates share one budget."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_tree.py"
    spec = importlib.util.spec_from_file_location("karox_release_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RELEASE_GATE = _load_release_gate()


def _module_exists(module: str) -> bool:
    parts = module.split(".")
    candidate = PACKAGE.joinpath(*parts)
    if (candidate / "__init__.py").is_file():
        return True
    return PACKAGE.joinpath(*parts[:-1], parts[-1] + ".py").is_file()


def test_no_legacy_module_returned() -> None:
    present = {path.name for path in PACKAGE.iterdir() if path.is_file()}
    assert present.isdisjoint(LEGACY)


def test_engine_stays_the_pruned_vendored_closure() -> None:
    engine = PACKAGE / "engine"
    present = {
        str(path.relative_to(engine)).replace("\\", "/")
        for path in engine.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert present == ENGINE_MODULES


def test_data_directories_carry_no_generated_cache() -> None:
    # Runtime imports legitimately create __pycache__ next to modules; the
    # migration mistake to catch is a cache copied into the package data that
    # ships in the wheel.
    for name in ("prompts", "builtin_skills"):
        cached = [
            path
            for path in (PACKAGE / name).rglob("*.pyc")
            if not path.name.startswith("__init__.")
        ]
        assert cached == []


def test_every_internal_import_resolves() -> None:
    missing: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        module = path.relative_to(PACKAGE).with_suffix("")
        parts = list(module.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        package = ["karox", *parts] if path.name == "__init__.py" else ["karox", *parts[:-1]]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = package[: len(package) - node.level + 1]
                if node.module is None:
                    # `from . import name` is either a submodule or a symbol
                    # defined by __init__.py; dunders are symbols.
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
                if not _module_exists(short):
                    missing.append(f"{module} -> {short}")
    assert missing == []


def test_release_has_no_hosted_bridge_import() -> None:
    text = (PACKAGE / "mcp_client.py").read_text(encoding="utf-8")
    assert "hosted_bridge" not in text


def test_python_line_count_stays_bounded() -> None:
    # One policy: the counter and the ceiling are the release gate's own, so
    # this test cannot drift to a second number again (80k in
    # scripts/check_tree.py versus 87k here disagreed until 2026-11-06).
    lines = _RELEASE_GATE.package_line_count(PACKAGE)
    problem = _RELEASE_GATE.line_budget_problem(lines)
    assert problem is None, problem


def test_release_gate_and_tree_test_use_one_budget() -> None:
    # Positive regression: the pytest guard and the script resolve the same
    # package, count it the same way and compare against one declared ceiling.
    # Raising the declared ceiling is a policy decision: update the value in
    # scripts/check_tree.py and this assertion together, and record why.
    lines = _RELEASE_GATE.package_line_count(PACKAGE)
    independent = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in PACKAGE.rglob("*.py")
    )
    assert _RELEASE_GATE.PACKAGE == PACKAGE
    assert lines == independent
    assert _RELEASE_GATE.MAX_PACKAGE_LINES == 96_000  # code mode (2026-11-14), owner-approved growth
    assert _RELEASE_GATE.MAX_PACKAGE_LINES > lines
    assert _RELEASE_GATE.line_budget_problem(independent) is None


def test_line_budget_fails_at_a_lower_test_only_ceiling() -> None:
    # Negative regression: the real measured package against a test-only
    # ceiling equal to its own count must fail closed, and one line more must
    # pass. The declared budget and the project are not touched.
    lines = _RELEASE_GATE.package_line_count(PACKAGE)
    failure = _RELEASE_GATE.line_budget_problem(lines, ceiling=lines)
    assert failure == f"package grew to {lines} lines (ceiling {lines})"
    assert _RELEASE_GATE.line_budget_problem(lines, ceiling=lines + 1) is None
