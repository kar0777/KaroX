"""What real OpenAI-compatible servers send and refuse, and Anthropic's cache markers."""

import asyncio
import json

import httpx
import pytest

from karox6.core.messages import Message, ToolCall
from karox6.providers.anthropic import AnthropicProvider
from karox6.providers.base import ModelRequest, ProviderError, ToolSpec
from karox6.providers.openai_chat import OpenAIChatProvider

READ = {"file_path": "a"}


def sse(*chunks):
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"


def delta(**fields):
    finish = fields.pop("finish", None)
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": finish}]}


def call(arguments, **extra):
    fn = {"name": "read", "arguments": arguments}
    return {"tool_calls": [{"type": "function", "function": fn, **extra}]}


def plain_request():
    return ModelRequest("m", "sys", [Message("user", "hi")])


def serve(answers, request=None, calls=1, on_delta=None):
    """``calls`` completions against a server that gives ``answers`` in order (a str is a
    200 SSE body, a tuple is (status, body)): (last result or error, bodies, provider)."""
    bodies = []

    def handler(http_request):
        bodies.append(json.loads(http_request.content))
        answer = answers[min(len(bodies), len(answers)) - 1]
        if isinstance(answer, tuple):
            return httpx.Response(answer[0], text=answer[1])
        return httpx.Response(200, text=answer)

    provider = OpenAIChatProvider("https://example.test/v1")

    async def go():
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = None
        try:
            for _ in range(calls):
                result = await provider.complete(request or plain_request(), on_delta)
            return result
        except ProviderError as exc:
            return exc
        finally:
            await provider.aclose()

    return asyncio.run(go()), bodies, provider


USAGE = {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 1}}
OK = sse(delta(content="ok", finish="stop"), USAGE)


def refusal(message):
    body = {"error": {"message": message, "type": "invalid_request_error"}}
    return (400, json.dumps(body))


@pytest.mark.parametrize(
    ("message", "check"),
    [
        (  # OpenAI reasoning models
            "Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead.",
            lambda b: b.get("max_completion_tokens") == 500 and "max_tokens" not in b,
        ),
        (
            "Unsupported value: 'temperature' does not support 0.2 with this model. "
            "Only the default (1) value is supported.",
            lambda b: "temperature" not in b,
        ),
        ("Unknown parameter: 'stream_options'.", lambda b: "stream_options" not in b),
        ("Extra inputs are not permitted: reasoning_effort", lambda b: "reasoning_effort" not in b),
    ],
)
def test_a_refused_request_field_is_dropped_and_the_request_sent_again(message, check):
    request = plain_request()
    request.max_output_tokens, request.temperature, request.reasoning_effort = 500, 0.2, "high"
    result, bodies, _ = serve([refusal(message), OK], request)
    assert not isinstance(result, ProviderError)
    assert result.message.content == "ok"
    assert len(bodies) == 2
    assert not check(bodies[0])
    assert check(bodies[1])


def test_several_refusals_are_learned_one_after_another_and_remembered():
    request = plain_request()
    request.max_output_tokens, request.temperature = 500, 0.2
    answers = [
        refusal("Unsupported parameter: max_tokens; use max_completion_tokens"),
        refusal("Unsupported value: temperature"),
        OK,
        OK,
    ]
    result, bodies, provider = serve(answers, request, calls=2)
    assert not isinstance(result, ProviderError)
    assert provider.quirks == {"max_completion_tokens", "no_temperature"}
    assert len(bodies) == 4  # two refusals, the answer, and a second call that starts right
    assert "temperature" not in bodies[3]
    assert bodies[3]["max_completion_tokens"] == 500


@pytest.mark.parametrize(
    "message",
    [
        "Invalid value for messages[2].content",  # not about a field we can drop
        "temperature must be between 0 and 2",  # a bad value: the user should see it
        "model not found",
    ],
)
def test_other_bad_requests_are_not_retried(message):
    result, bodies, _ = serve([refusal(message), OK])
    assert isinstance(result, ProviderError)
    assert result.code == "bad_request"
    assert len(bodies) == 1


def test_a_refusal_that_repeats_ends_instead_of_looping():
    again = refusal("Unknown parameter: stream_options")
    result, bodies, _ = serve([again, again, OK])
    assert isinstance(result, ProviderError)
    assert len(bodies) == 2  # learned once; the same answer again is final


# -- reasoning, tool calls and usage as each server spells them ----------------------

FULL = json.dumps(READ)
CACHED = {"prompt_tokens_details": {"cached_tokens": 800}}
COUNTED = {"completion_tokens_details": {"reasoning_tokens": 40}}
BASE = {"prompt_tokens": 1000, "completion_tokens": 60}
DIALECTS = {
    # DeepSeek: reasoning_content, arguments in pieces, its own cache counters
    "deepseek": (
        [
            delta(reasoning_content="think "),
            delta(reasoning_content="more"),
            delta(**call("", index=0, id="c1")),
            delta(tool_calls=[{"index": 0, "function": {"arguments": FULL}}]),
            delta(finish="tool_calls"),
            {"choices": [], "usage": {**BASE, "prompt_cache_hit_tokens": 800, **COUNTED}},
        ],
        (800, 40, None),
    ),
    # OpenRouter: the field is "reasoning"
    "openrouter": (
        [
            delta(reasoning="think more"),
            delta(**call(FULL, index=0, id="c1")),
            delta(finish="tool_calls"),
            {"choices": [], "usage": {**BASE, **CACHED, **COUNTED, "cost": 0.0001}},
        ],
        (800, 40, "reasoning"),
    ),
    # GLM (Zhipu / z.ai): the whole call in one chunk, usage beside the last choice
    "glm": (
        [
            delta(reasoning_content="think more"),
            {
                **delta(**call(FULL, index=0, id="c1"), finish="tool_calls"),
                "usage": {**BASE, **CACHED},
            },
        ],
        (800, 0, None),
    ),
    # Ollama / vLLM: no call index, "stop" beside a tool call, no cache or reasoning counts
    "local": (
        [
            delta(reasoning="think more"),
            delta(**call(FULL, id="call_x")),
            delta(finish="stop"),
            {"choices": [], "usage": BASE},
        ],
        (0, 0, "reasoning"),
    ),
}


@pytest.mark.parametrize("name", sorted(DIALECTS))
def test_dialects_of_openai_compatible_servers(name):
    chunks, (cached, reasoning, field) = DIALECTS[name]
    seen = []
    request = plain_request()
    request.tools = [ToolSpec("read", "read a file", {"type": "object"})]
    reply, bodies, _ = serve([sse(*chunks)], request, on_delta=seen.append)
    assert not isinstance(reply, ProviderError)
    assert bodies[0]["tools"][0]["function"]["name"] == "read"
    assert reply.finish_reason == "tool_calls"
    assert [(c.name, json.loads(c.arguments)) for c in reply.message.tool_calls] == [("read", READ)]
    assert reply.message.reasoning == "think more"
    assert "".join(d.text for d in seen if d.kind == "reasoning") == "think more"  # live
    usage = reply.usage
    assert (usage.input_tokens, usage.output_tokens) == (1000, 60)
    assert usage.cached_tokens == cached
    assert usage.reasoning_tokens == reasoning
    assert reply.message.extra.get("reasoning_field") == field


def test_reasoning_goes_back_in_the_field_the_server_used():
    turn = Message(
        "assistant",
        "",
        tool_calls=[ToolCall("c1", "read", "{}")],
        reasoning="why",
        extra={"reasoning_field": "reasoning"},
    )
    history = [Message("user", "hi"), turn, Message("tool", "result", tool_call_id="c1")]
    _, bodies, _ = serve([OK], ModelRequest("m", "sys", history))
    assistant = bodies[0]["messages"][2]
    assert assistant["reasoning"] == "why"
    assert "reasoning_content" not in assistant


# -- Anthropic: where the cache breakpoints sit ---------------------------------------


def anthropic_body(messages):
    tools = [ToolSpec("read", "read a file", {"type": "object"})]
    request = ModelRequest("claude", "system prompt", messages, tools=tools)
    return AnthropicProvider("https://example.test").build_body(request)


def marked(body):
    return [
        (i, j)
        for i, message in enumerate(body["messages"])
        for j, block in enumerate(message["content"])
        if "cache_control" in block
    ]


def test_anthropic_marks_the_last_two_user_turns_and_the_system_prompt():
    calls = [ToolCall(f"c{i}", "read", "{}") for i in range(30)]  # one step, 30 parallel reads
    history = [
        Message("user", "task"),
        Message("assistant", "", tool_calls=calls),
        *[Message("tool", f"result {i}", tool_call_id=f"c{i}") for i in range(30)],
    ]
    body = anthropic_body(history)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The end of the conversation (written now) and the end of the user turn before it,
    # where the previous request wrote its entry: the 30 results alone are more blocks
    # than the API looks back from a new breakpoint, so without the second mark the whole
    # conversation was written to the cache again.
    assert marked(body) == [(0, 0), (2, 29)]
    assert len(marked(body)) + 1 <= 4  # the API allows four breakpoints
    earlier = anthropic_body(history[:1])
    assert marked(earlier) == [(0, 0)]  # the previous request's entry ends at that block
    assert body["messages"][0]["content"][0] == earlier["messages"][0]["content"][0]
