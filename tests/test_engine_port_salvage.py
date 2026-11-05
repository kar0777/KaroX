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
from karox.engine_salvage import salvage_fenced_command


def make_response(content: str, *, finish_reason: str = "stop", tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ]
    )


def make_model():
    """The product bridge, not the vendored litellm transport."""
    from karox.engine_kernel import EngineProviderModel
    from karox.providers import ModelResponse

    return EngineProviderModel(
        provider=_single_response(
            ModelResponse(
                content="",
                tool_calls=(),
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )
        ),
        model_id="race-alias",
        tools=(),
        event=lambda kind, **fields: None,
        deadline_seconds=30.0,
    )


def _single_response(response):
    class _Provider:
        def complete(self, request):
            return response

    return _Provider()


def _model_response(content: str, *, finish_reason: str = "stop", tool_calls=()):
    from karox.providers import ModelResponse

    return ModelResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        finish_reason=finish_reason,
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )


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
    actions = model._parse_actions(
        _model_response("Let me fix it.\n\n```mswea_bash_command\nmake test\n```")
    )
    assert actions == [{"command": "make test"}]


def test_parse_actions_still_raises_without_a_command():
    model = make_model()
    with pytest.raises(FormatError):
        model._parse_actions(_model_response("The solution is complete."))


def test_tool_calls_win_over_a_fence():
    from karox.providers import ToolCall

    model = make_model()
    tool_call = ToolCall(
        call_id="call_1", name="finish", raw_arguments='{"answer": "done"}'
    )
    actions = model._parse_actions(
        _model_response(
            "```mswea_bash_command\npwd\n```",
            finish_reason="tool_calls",
            tool_calls=(tool_call,),
        )
    )
    assert actions == [
        {"name": "finish", "arguments": {"answer": "done"}, "tool_call_id": "call_1"}
    ]

# -- leaked tool-call markup (r4 eng-task-queue) ---------------------------

from karox.engine_salvage import salvage_markup_tool_calls  # noqa: E402

_BAR = "\uff5c"
_R4_PROBE = (
    "\n\n<{b}DSML{b} calls>\n"
    '<{b}DSML{b} invoke name="checks_run">\n'
    '<{b}DSML{b} parameter name="argv" string="false">'
    '["python", "-c", "q.enqueue({{\'a\': [1]}})\\nprint(\'dead\', q.dead_letters()[0].payload)\\n"]'
    "</{b}DSML{b} parameter>\n"
    "</{b}DSML{b} invoke>\n"
    "</{b}DSML{b} calls>"
).format(b=_BAR)
_PROBE_ARGV = [
    "python",
    "-c",
    "q.enqueue({'a': [1]})\nprint('dead', q.dead_letters()[0].payload)\n",
]


def _tool(name, properties):
    from karox.providers import ProviderTool

    return ProviderTool(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": properties},
    )


_TOOLS = (
    _tool("checks_run", {"argv": {"type": "array", "items": {"type": "string"}}}),
    _tool("repo_write_file", {"path": {"type": "string"}, "content": {"type": "string"}}),
)


def _bridge(response):
    from karox.engine_kernel import EngineProviderModel

    return EngineProviderModel(
        provider=_single_response(response),
        model_id="race-alias",
        tools=_TOOLS,
        event=lambda kind, **fields: None,
        deadline_seconds=30.0,
    )


def _query(response):
    return _bridge(response).query(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    )


def test_dsml_markup_as_the_gateway_leaked_it_is_a_call():
    calls, rest = salvage_markup_tool_calls(_R4_PROBE, {"checks_run"})
    assert calls == [("checks_run", {"argv": _PROBE_ARGV})]
    assert rest == ""


def test_prose_around_leaked_markup_is_kept_and_the_markup_removed():
    calls, rest = salvage_markup_tool_calls("Probe the edges." + _R4_PROBE, {"checks_run"})
    assert [name for name, _ in calls] == ["checks_run"]
    assert rest == "Probe the edges."


def test_markup_naming_no_known_tool_stays_text():
    text = _R4_PROBE.replace("checks_run", "run_everything")
    assert salvage_markup_tool_calls(text, {"checks_run"}) == ([], text)


def test_markup_cut_off_mid_call_stays_text():
    text = _R4_PROBE.split("</" + _BAR + "DSML" + _BAR + " parameter>")[0]
    assert salvage_markup_tool_calls(text, {"checks_run"}) == ([], text)


def test_text_parameters_stay_text_and_json_ones_are_decoded():
    text = (
        '<invoke name="repo_write_file">'
        '<parameter name="path" string="true">a.json</parameter>'
        '<parameter name="content">{"a": 1}</parameter>'
        "</invoke>"
    )
    schemas = {tool.name: tool.input_schema for tool in _TOOLS}
    calls, rest = salvage_markup_tool_calls(text, {"repo_write_file"}, schemas)
    assert calls == [("repo_write_file", {"path": "a.json", "content": '{"a": 1}'})]
    assert rest == ""


def test_hermes_tool_call_markup_is_a_call():
    text = 'Run it.\n<tool_call>\n{"name": "checks_run", "arguments": {"argv": ["pytest", "-q"]}}\n</tool_call>'
    calls, rest = salvage_markup_tool_calls(text, {"checks_run"})
    assert calls == [("checks_run", {"argv": ["pytest", "-q"]})]
    assert rest == "Run it."


def test_a_leaked_call_beside_a_parsed_one_runs_after_it():
    """r4 eng-task-queue step 3: pytest was parsed, the probe beside it dropped."""
    from karox.providers import ToolCall

    parsed = ToolCall(call_id="call_1", name="checks_run", raw_arguments='{"argv":["python","-m","pytest","-q"]}')
    message = _query(_model_response(_R4_PROBE, finish_reason="tool_calls", tool_calls=(parsed,)))
    actions = message["extra"]["actions"]
    assert [action["arguments"] for action in actions] == [
        {"argv": ["python", "-m", "pytest", "-q"]},
        {"argv": _PROBE_ARGV},
    ]
    assert [action["batch_index"] for action in actions] == [0, 1]
    assert [call["call_id"] for call in message["tool_calls"]][0] == "call_1"
    assert message["tool_calls"][1]["call_id"] == actions[1]["tool_call_id"]
    assert "DSML" not in (message["content"] or "")
    assert message["extra"]["salvaged_markup_calls"] == 1


def test_a_response_that_is_only_leaked_markup_is_not_a_format_error():
    message = _query(_model_response(_R4_PROBE))
    assert [(a["name"], a["arguments"]) for a in message["extra"]["actions"]] == [
        ("checks_run", {"argv": _PROBE_ARGV})
    ]


def test_a_call_the_gateway_already_parsed_does_not_run_twice():
    from karox.providers import ToolCall

    parsed = ToolCall(call_id="call_1", name="checks_run", raw_arguments=json_dumps({"argv": _PROBE_ARGV}))
    message = _query(_model_response(_R4_PROBE, finish_reason="tool_calls", tool_calls=(parsed,)))
    assert len(message["extra"]["actions"]) == 1
    assert "salvaged_markup_calls" not in message["extra"]
    assert message["content"] == ""


def test_unsalvageable_markup_is_never_accepted_as_the_final_answer():
    from karox.engine.agents.default import DefaultAgent

    truncated = _R4_PROBE.split("</" + _BAR + "DSML" + _BAR + " parameter>")[0]
    error = FormatError(
        {
            "role": "user",
            "content": "Format error",
            "extra": {
                "response": {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": truncated}}
                    ]
                }
            },
        }
    )
    assert DefaultAgent._final_answer_text(error) == ""


def json_dumps(value):
    import json

    return json.dumps(value)
