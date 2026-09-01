"""Human-facing argument normalization for the KaroX command line.

The canonical argparse tree stays explicit and script-friendly. This module adds
only deterministic aliases before argparse sees argv, so users can type the same
short nouns and task-first commands they see in the TUI without maintaining a
second CLI implementation.
"""

from __future__ import annotations

from collections.abc import Sequence

_ROOT_ALIASES = {
    "models": "model",
    "providers": "provider",
    "sessions": "session",
    "agents": "intelligence",
    "mission": "mission-control",
    "skills": "skill",
    "packs": "pack",
    "targets": "target",
    "tools": "tool",
    "integrations": "integration",
}

_DEFAULT_LIST_ROOTS = frozenset(
    {
        "model", "models", "provider", "providers", "session", "sessions",
        "intelligence", "agents", "skill", "skills", "pack", "packs",
        "target", "targets", "tool", "tools", "integration", "integrations",
    }
)

_ORCHESTRATION_ACTIONS = frozenset(
    {
        "recipes", "recipe-add", "recipe-remove", "plan", "run", "start",
        "observe", "shadow-route", "status", "recover", "resolve-recovery",
    }
)


def _default_list(arguments: list[str], root: str) -> None:
    if root in _DEFAULT_LIST_ROOTS and (len(arguments) == 1 or arguments[1].startswith("-")):
        arguments.insert(1, "list")


def _model_shortcuts(arguments: list[str], root: str) -> None:
    if root not in {"model", "models"} or len(arguments) <= 1:
        return
    action = arguments[1].casefold()
    if action == "use":
        arguments[1] = "select"
    elif action == "auto":
        arguments[1] = "repair-selection"
    elif action == "refresh":
        arguments[1] = "discover"
        if len(arguments) > 2 and not arguments[2].startswith("-"):
            provider_id = arguments.pop(2)
            arguments[2:2] = ["--provider", provider_id]


def _agent_shortcuts(arguments: list[str], root: str) -> None:
    if root not in {"agents", "intelligence"} or len(arguments) <= 1:
        return
    action = arguments[1].casefold()
    if action == "apply":
        arguments[1:2] = ["discover-agents", "--apply"]
    elif action == "refresh":
        arguments[1] = "discover-agents"


def _mission_shortcut(arguments: list[str], root: str) -> None:
    if root not in {"mission", "mission-control"} or len(arguments) <= 1:
        return
    if arguments[1].casefold() not in {"show", "command", "serve"} and not arguments[1].startswith("-"):
        arguments.insert(1, "show")


def _orchestration_shortcut(arguments: list[str], root: str) -> None:
    if root != "orchestrate" or len(arguments) <= 1:
        return
    action = arguments[1].casefold()
    if action not in _ORCHESTRATION_ACTIONS and not arguments[1].startswith("-"):
        arguments.insert(1, "run")
        action = "run"
    if action not in {"plan", "run", "start"} or "--objective" in arguments:
        return

    text_start = 2
    delegated_endpoint: str | None = None
    if text_start < len(arguments):
        first = arguments[text_start]
        if first.startswith("@") and len(first) > 1:
            delegated_endpoint = first[1:]
            arguments.pop(text_start)

    text_end = text_start
    while text_end < len(arguments) and not arguments[text_end].startswith("-"):
        text_end += 1
    if text_end <= text_start:
        return
    objective = " ".join(arguments[text_start:text_end]).strip()
    if not objective:
        return
    arguments[text_start:text_end] = ["--objective", objective]
    if delegated_endpoint is not None:
        if "--orchestrator" not in arguments:
            arguments.extend(("--orchestrator", delegated_endpoint))
        if "--delegate-workers" not in arguments and "--no-delegate-workers" not in arguments:
            arguments.append("--delegate-workers")


def normalize_cli_arguments(arguments: Sequence[str]) -> list[str]:
    """Return canonical argv while preserving every explicit legacy spelling."""
    normalized = [str(item) for item in arguments]
    if not normalized:
        return normalized
    root = normalized[0].casefold()
    _default_list(normalized, root)
    _model_shortcuts(normalized, root)
    _agent_shortcuts(normalized, root)
    _mission_shortcut(normalized, root)
    _orchestration_shortcut(normalized, root)
    normalized[0] = _ROOT_ALIASES.get(root, normalized[0])
    return normalized


__all__ = ["normalize_cli_arguments"]
