from __future__ import annotations

from karox import cli


def test_root_help_is_task_first_and_hides_admin_catalog(capsys) -> None:
    assert cli.main(["--help"]) == 0
    text = capsys.readouterr().out
    for useful in (
        "karox models",
        "karox effort",
        "karox mode",
        "karox sessions",
        "karox agents",
        "karox orchestrate TASK",
        "karox doctor",
        "karox help all",
    ):
        assert useful in text
    for internal in (
        "browser-credential",
        "mission-control",
        "credential          manage",
        "mcp                 manage",
        "bridge              manage",
    ):
        assert internal not in text


def test_help_all_preserves_complete_argparse_catalog(capsys) -> None:
    assert cli.main(["help", "all"]) == 0
    text = capsys.readouterr().out
    for advanced in (
        "browser-credential",
        "mission-control",
        "credential",
        "mcp",
        "bridge",
        "orchestrate",
    ):
        assert advanced in text


def test_compatibility_normalizer_delegates_to_full_shortcut_layer() -> None:
    assert cli._normalize_root_arguments(["models"]) == ["model", "list"]
    assert cli._normalize_root_arguments(["mission", "run-1"]) == [
        "mission-control",
        "show",
        "run-1",
    ]
    assert cli._normalize_root_arguments(["orchestrate", "fix", "the", "tests"]) == [
        "orchestrate",
        "run",
        "--objective",
        "fix the tests",
    ]
