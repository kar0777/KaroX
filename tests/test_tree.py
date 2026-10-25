"""The release tree is a contract, not whatever happened to be copied.

These tests are the cheap, deterministic half of release hygiene: no legacy
module can quietly reappear, no cache can be committed, and every intra-package
import must resolve inside the release.
"""

from __future__ import annotations

import ast
from pathlib import Path

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
}


def _module_exists(module: str) -> bool:
    parts = module.split(".")
    candidate = PACKAGE.joinpath(*parts)
    if (candidate / "__init__.py").is_file():
        return True
    return PACKAGE.joinpath(*parts[:-1], parts[-1] + ".py").is_file()


def test_no_legacy_module_returned() -> None:
    present = {path.name for path in PACKAGE.iterdir() if path.is_file()}
    assert present.isdisjoint(LEGACY)


def test_data_directories_carry_no_generated_cache() -> None:
    # Runtime imports legitimately create __pycache__ next to modules; the
    # migration mistake to catch is a cache copied into the package data that
    # ships in the wheel.
    for name in ("prompts", "builtin_skills", "skill_notes"):
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
    lines = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in PACKAGE.rglob("*.py")
    )
    # The kernel is large by design; the guard is for accumulation of new
    # surface, not for the kernel itself. 70k covered the release kernel with
    # the hosted MCP bridge. The seed race commit vendored the karo-core
    # engine (~5.6k lines, src/karox/engine) and B1 added its typed-tool
    # adapter (engine_kernel.py), which is deliberate product surface, so the
    # bound moved to 80k. Raise it deliberately, never to make a stray copy
    # pass.
    assert lines < 80_000