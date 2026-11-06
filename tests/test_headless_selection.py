"""Headless quick gate: an explicit allowlist, not a broad pytest selection.

On 2026-11-06 a whole-module selection pulled in `tests/test_engine_tools.py`
and, because managed browser support reported itself ready, three scenarios
started a real browser against a loopback fixture. These checks pin the
replacement selection: every file below was read before admission, the gate is
browser-free by construction, and the three incident tests are proven absent.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

#: The offline/headless quick gate. Every entry was read before admission: none
#: imports a browser driver, opens a page or a listening socket, calls a
#: provider or needs an external address.
ALLOWLIST = (
    "test_tree.py",
    "test_tool_shape_fixes.py",
    "test_session_export.py",
    "test_agent_prompt.py",
    "test_agent_report_surface.py",
    "test_engine_provider_accounting.py",
    "test_engine_tool_env_render.py",
    "test_run_reuse_identity.py",
    "test_skill_scope.py",
    "test_unified_patch_leniency.py",
    "test_package_data.py",
    "test_headless_selection.py",
)

#: The three tests that started a real browser in the Mission 07 run. They live
#: only in `tests/test_engine_tools.py`, which is excluded by name.
MISSION07_BROWSER_TESTS = (
    "test_cli_engine_run_advertises_and_executes_browser_command",
    "test_browser_command_entry_point_opens_a_real_page",
    "test_cli_engine_run_logs_in_and_downloads_a_report",
)

#: Static surface that must not appear in a browser-free module. Transitive
#: product imports are not a browser run; this catches a direct one.
FORBIDDEN_SOURCE_TOKENS = (
    "playwright",
    "chromium",
    "selenium",
    "karox.browser",
    "karox.computer_use",
    "http://",
    "https://",
)

DOC_HEADING = "## Offline headless quick gate"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end == -1 else rest[:end]


def _fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def test_every_allowlisted_module_exists_and_is_a_test() -> None:
    assert len(ALLOWLIST) == len(set(ALLOWLIST))
    for name in ALLOWLIST:
        assert name.startswith("test_") and name.endswith(".py"), name
        assert (TESTS / name).is_file(), f"allowlisted test is missing: {name}"


def test_mission07_browser_tests_are_not_part_of_the_allowlist() -> None:
    assert "test_engine_tools.py" not in ALLOWLIST
    definitions = "\n".join(
        line
        for name in ALLOWLIST
        for line in (TESTS / name).read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("def test_")
    )
    incident_source = (TESTS / "test_engine_tools.py").read_text(encoding="utf-8")
    for name in MISSION07_BROWSER_TESTS:
        assert f"def {name}(" not in definitions, f"{name} leaked into the quick gate"
        assert f"def {name}(" in incident_source, f"negative control drifted: {name}"


def test_allowlisted_modules_do_not_import_a_browser_driver() -> None:
    for name in ALLOWLIST:
        forbidden = sorted(
            module
            for module in _imported_modules(TESTS / name)
            if module.startswith(
                ("playwright", "selenium", "karox.browser", "karox.computer_use")
            )
        )
        assert forbidden == [], (name, forbidden)


def test_allowlisted_sources_have_no_browser_or_external_url_surface() -> None:
    guard = Path(__file__).name
    for name in ALLOWLIST:
        if name == guard:
            # This file names the forbidden tokens in order to assert their absence.
            continue
        text = (TESTS / name).read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_SOURCE_TOKENS:
            assert token not in text, f"{name} contains {token!r}"


def test_docs_command_lists_exactly_the_allowlist() -> None:
    docs = (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    section = _section(docs, DOC_HEADING)
    commands = [block for block in _fenced_blocks(section) if "pytest" in block]
    assert len(commands) == 1, commands
    command = commands[0]
    listed = {
        Path(token).name
        for token in re.findall(r"tests/test_[A-Za-z0-9_]+\.py", command)
    }
    assert listed == set(ALLOWLIST)
    assert '-o addopts=""' in command
    assert "-p no:cacheprovider" in command
