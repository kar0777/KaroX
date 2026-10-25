"""karo-core text-command salvage: a fenced command is still an action.

Port 5 of the KaroX task loop. Evidence: jsonargparse-707 v3 -- after 27 real
steps the model answered "Now I understand the bug... Let me implement the fix"
with the patch script in a ```mswea_bash_command fence and no tool call. Three
such responses in a row ended the run with the patch never applied. One
bash-ish fence is unambiguous, so it is executed; two fences, or a fence in
another language, remain a format error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from karox.engine.exceptions import FormatError
from karox.engine.models.litellm_model import LitellmModel, salvage_fenced_command


def make_response(content: str, *, finish_reason: str = "stop", tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ]
    )


def make_model() -> LitellmModel:
    return LitellmModel(model_name="openai/race-alias", model_kwargs={})


def test_salvage_single_mswea_fence():
    content = 'Now I understand the bug. Let me apply it.\n\n```mswea_bash_command\npython - <<\'EOF\'\nprint(1)\nEOF\n```'
    assert salvage_fenced_command(content) == "python - <<'EOF'\nprint(1)\nEOF"


def test_salvage_accepts_bash_and_sh():
    assert salvage_fenced_command("```bash\nls -la\n```") == "ls -la"
    assert salvage_fenced_command("```sh\npytest -q\n```") == "pytest -q"


def test_two_fences_are_a_format_error():
    assert salvage_fenced_command("```bash\nls\n```\nand\n```bash\npwd\n```") is None


def test_non_shell_fence_is_a_format_error():
    assert salvage_fenced_command("```python\nprint('hi')\n```") is None


def test_empty_fence_is_a_format_error():
    assert salvage_fenced_command("```bash\n\n```") is None


def test_unclosed_fence_is_salvaged():
    """sqlglot-4935 v4h: the response stops mid-command at a leaked tag."""
    content = 'Let me reproduce.\n\n```mswea_bash_command\ncd / && python /tmp/repro.py</parameter>\n</invoke>'
    assert salvage_fenced_command(content) == "cd / && python /tmp/repro.py"


def test_unclosed_fence_without_tag_is_salvaged():
    content = 'Let me check.\n\n```mswea_bash_command\npwd && ls'
    assert salvage_fenced_command(content) == "pwd && ls"


def test_one_closed_fence_wins_over_a_later_open_one():
    content = "```bash\nls\n```\ntext\n```bash\npwd"
    assert salvage_fenced_command(content) == "ls"


def test_parse_actions_returns_the_fenced_command():
    model = make_model()
    actions = model._parse_actions(make_response('Let me fix it.\n\n```mswea_bash_command\nmake test\n```'))
    assert actions == [{"command": "make test"}]


def test_parse_actions_still_raises_without_a_command():
    model = make_model()
    with pytest.raises(FormatError):
        model._parse_actions(make_response("The solution is complete."))


def test_tool_calls_win_over_a_fence():
    model = make_model()
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="bash", arguments='{"command": "ls"}'),
    )
    actions = model._parse_actions(
        make_response("```mswea_bash_command\npwd\n```", finish_reason="tool_calls", tool_calls=[tool_call])
    )
    assert actions == [{"command": "ls", "tool_call_id": "call_1"}]