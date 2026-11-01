"""Model-facing instructions stay task-neutral.

KaroX's own instructions -- the system prompt, the visual-checker prompt, the
built-in skills and the strings the runtime puts in front of the model -- are
read on every run. They may teach how to verify; they must not carry what one
evaluation task checks. Two leaks made that concrete: a probe expression copied
from a hidden check (``state().obstacles.length > 0`` after ``reset()``) and
checker prose assembled from two fixtures' nouns.

The detectors are structural -- code-level state assertions, harness
vocabulary, pass fractions and evaluation task ids -- plus the fixture nouns
that already leaked once. Each detector is tested on the removed text and on
neutral prose, so a pattern that silently stops matching fails here first.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from karox.agent import SYSTEM_PROMPT

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "karox"

#: Modules whose non-docstring string literals reach the model: the prompt,
#: tool descriptions, observation hints, gate and repair messages.
MODEL_FACING_MODULES = (
    "agent.py",
    "engine_kernel.py",
    "engine_tool_env.py",
    "completion_gate.py",
    "visual_check.py",
    "tool_schemas.py",
    "project_context.py",
    "state_briefing.py",
)

#: A property of a call result compared with a literal, such as
#: ``state().items.length > 0``. "Check the count the task requires" is a rule;
#: a concrete expression is one task's test.
CODE_STATE_ASSERTION = re.compile(
    r"[A-Za-z_$][\w$]*\(\)(?:\.[A-Za-z_$][\w$]*)+\s*(?:===?|!==?|>=?|<=?)\s*[\w.'\"-]+"
)
#: An inline code span that holds a comparison.
CODE_SPAN_COMPARISON = re.compile(r"`[^`\n]*(?:===?|!==?|>=|<=|\s>\s|\s<\s)[^`\n]*`")
#: Evaluation-harness vocabulary and pass fractions such as "29/30".
HARNESS_MARKER = re.compile(
    r"\b(?:arena|graders?|hidden (?:tests?|checks?)|kernel-duel|__gameTest)\b"
    r"|\b\d{1,3}/\d{1,3}\b",
    re.IGNORECASE,
)
#: Ids of the evaluation tasks this runtime is measured on, and the SWE-bench
#: instance shape.
TASK_ID = re.compile(
    r"\b(?:3d-game|3d-viewer|web-snake|web-platformer|web-dashboard|web-data-viz|"
    r"web-landing|eng-csv-report|eng-diff-patch|eng-jsonl-store|eng-lru-ttl|"
    r"eng-rate-limit|eng-task-queue)\b|\bswe-[a-z]+-\d+\b",
    re.IGNORECASE,
)
#: Fixture nouns that already leaked into instruction prose once.
FIXTURE_NOUN = re.compile(
    r"\b(?:aquariums?|fish\w*|tanks?|gravel|bubbles?|sand|hoods?|neon|lanes?|"
    r"rails?|ships?|obstacles?|coins?|collectibles?)\b",
    re.IGNORECASE,
)

STRUCTURAL = (CODE_STATE_ASSERTION, CODE_SPAN_COMPARISON, HARNESS_MARKER, TASK_ID)
PROSE = STRUCTURAL + (FIXTURE_NOUN,)

REMOVED_PROBE = (
    "- When the task exposes test hooks, verify that ready state with a probe that asserts "
    "`state().obstacles.length > 0` and `state().coins.length > 0` after `reset()`; an empty "
    "array is a failure even when score, overlay and status are correct."
)
REMOVED_RULE = (
    "- For a game, the ready/initial state must already show the first obstacles and "
    "collectibles the player will meet; create them in reset()/initialization."
)
REMOVED_CHECKER = (
    "do not demand a particular ship silhouette, and do not fail a dark neon floor whose "
    "lane lines and rails are visible"
)
NEUTRAL = (
    "- Verify behaviour with probes that check concrete, observable facts the task requires "
    "(values, counts, visible text, state transitions); a check that compares a value to "
    "itself, or that cannot fail, proves nothing. After initialization or a reset, confirm "
    "that the state matches the initial conditions the task specifies.\n"
    "- Handle resize with `renderer.setSize(width, height)` and keep near/far tight; "
    "planes, guardrails and relationships are ordinary words."
)


def _hits(patterns: tuple[re.Pattern[str], ...], text: str) -> list[str]:
    return [match.group(0) for pattern in patterns for match in pattern.finditer(text)]


def _prose_sources() -> list[tuple[str, str]]:
    sources = [("karox.agent.SYSTEM_PROMPT", SYSTEM_PROMPT)]
    sources += [
        (f"prompts/{path.name}", path.read_text(encoding="utf-8"))
        for path in sorted((PACKAGE / "prompts").glob("*.md"))
    ]
    sources += [
        (f"builtin_skills/{path.parent.name}/SKILL.md", path.read_text(encoding="utf-8"))
        for path in sorted((PACKAGE / "builtin_skills").glob("*/SKILL.md"))
    ]
    return sources


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _module_literals(module: str) -> list[tuple[str, str]]:
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    skip = _docstrings(tree)
    return [
        (f"{module}:{node.lineno}", node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def test_detectors_flag_the_removed_benchmark_text() -> None:
    assert CODE_STATE_ASSERTION.search(REMOVED_PROBE)
    assert CODE_SPAN_COMPARISON.search(REMOVED_PROBE)
    assert FIXTURE_NOUN.search(REMOVED_RULE)
    assert FIXTURE_NOUN.search(REMOVED_CHECKER)
    assert HARNESS_MARKER.search("verified at 29/30 on the hidden grader")
    assert HARNESS_MARKER.search("an arena run")
    assert TASK_ID.search("the 3d-game and swe-requests-6028 runs")


def test_detectors_accept_task_neutral_prose() -> None:
    assert _hits(PROSE, NEUTRAL) == []


def test_the_scan_covers_every_instruction_source() -> None:
    names = [name for name, _ in _prose_sources()]
    assert "karox.agent.SYSTEM_PROMPT" in names
    assert "prompts/visual_checker.md" in names
    assert "builtin_skills/web-3d-scene/SKILL.md" in names
    for module in MODEL_FACING_MODULES:
        assert _module_literals(module), module


def test_instruction_prose_is_task_neutral() -> None:
    findings = [
        f"{name}: {hit!r}" for name, text in _prose_sources() for hit in _hits(PROSE, text)
    ]
    assert not findings, findings


def test_model_facing_strings_carry_no_task_checks() -> None:
    findings = [
        f"{name}: {hit!r}"
        for module in MODEL_FACING_MODULES
        for name, text in _module_literals(module)
        for hit in _hits(STRUCTURAL, text)
    ]
    assert not findings, findings
