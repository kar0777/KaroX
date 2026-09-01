from __future__ import annotations

from karox.cli import _normalize_root_arguments


def test_plural_collection_shortcuts_default_to_list() -> None:
    expected = {
        "models": ["model", "list"],
        "providers": ["provider", "list"],
        "sessions": ["session", "list"],
        "agents": ["intelligence", "list"],
        "skills": ["skill", "list"],
        "packs": ["pack", "list"],
        "targets": ["target", "list"],
        "tools": ["tool", "list"],
        "integrations": ["integration", "list"],
    }
    for shortcut, expansion in expected.items():
        assert _normalize_root_arguments([shortcut]) == expansion


def test_plural_collection_shortcuts_keep_flags_after_default_list() -> None:
    assert _normalize_root_arguments(["models", "--json"]) == [
        "model",
        "list",
        "--json",
    ]
    assert _normalize_root_arguments(["sessions", "--all", "--json"]) == [
        "session",
        "list",
        "--all",
        "--json",
    ]


def test_plural_collection_shortcuts_preserve_explicit_subcommands() -> None:
    assert _normalize_root_arguments(["models", "discover", "openrouter"]) == [
        "model",
        "discover",
        "openrouter",
    ]
    assert _normalize_root_arguments(["agents", "show", "sub:codex"]) == [
        "intelligence",
        "show",
        "sub:codex",
    ]


def test_non_collection_alias_and_canonical_commands_are_unchanged() -> None:
    assert _normalize_root_arguments(["mission", "show", "run-1"]) == [
        "mission-control",
        "show",
        "run-1",
    ]
    assert _normalize_root_arguments(["model", "list"]) == ["model", "list"]
    assert _normalize_root_arguments([]) == []
