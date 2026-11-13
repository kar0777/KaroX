"""Prompt-prefix caching: the run's cache key and why a request missed."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx

from karox.engine_provider_bridge import EngineProviderModel
from karox.prompt_cache_telemetry import PromptPrefixTracker, summarize
from karox.provider_adapters import AnthropicMessagesProvider, OpenAIResponsesProvider
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpenAIChatCompletionsProvider,
    ProviderTool,
    ToolCall,
)


class _Recording:
    provider_name = "recording"

    def __init__(self, usages: List[Dict[str, int]]) -> None:
        self.requests: List[ModelRequest] = []
        self._usages = list(usages)

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        usage = self._usages.pop(0) if self._usages else {}
        return ModelResponse(
            content=None,
            tool_calls=(ToolCall(f"f{len(self.requests)}", "finish", json.dumps({"answer": "ok"})),),
            finish_reason="tool_calls",
            usage=usage,
        )

    def close(self) -> None:
        return None


_TOOLS = (ProviderTool("git_status", "status", {"type": "object", "properties": {}}),)


def _model(provider: Any, cache_key: Any = "karox-abc") -> EngineProviderModel:
    return EngineProviderModel(
        provider=provider,
        model_id="m",
        tools=_TOOLS,
        deadline_seconds=60.0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
        cache_key=cache_key,
    )


def _history(turns: int) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
    ]
    for index in range(turns):
        messages.append({"role": "assistant", "content": f"step {index}"})
        messages.append({"role": "user", "content": f"observation {index}"})
    return messages


def test_every_request_of_a_run_carries_the_same_cache_key() -> None:
    provider = _Recording([{}, {}])
    model = _model(provider)
    model.query(_history(0))
    model.query(_history(1))
    keys = [request.cache_key for request in provider.requests]
    assert keys[0] and keys[0] == keys[1]
    assert keys[0].startswith("karox-abc-")


def test_a_different_system_prompt_gets_a_different_key() -> None:
    provider = _Recording([{}, {}])
    model = _model(provider)
    model.query(_history(0))
    changed = _history(0)
    changed[0] = {"role": "system", "content": "another system prompt"}
    model.query(changed)
    assert provider.requests[0].cache_key != provider.requests[1].cache_key


def test_without_a_key_no_cache_key_is_sent() -> None:
    provider = _Recording([{}])
    _model(provider, cache_key=None).query(_history(0))
    assert provider.requests[0].cache_key is None


def test_the_engine_message_reports_why_a_large_prompt_missed() -> None:
    provider = _Recording(
        [
            {"prompt_tokens": 5000, "cache_read_tokens": 0},
            {"prompt_tokens": 6000, "cache_read_tokens": 4800},
            {"prompt_tokens": 7000, "cache_read_tokens": 0},
        ]
    )
    model = _model(provider)
    first = model.query(_history(0))
    second = model.query(_history(1))
    third = model.query(_history(2))
    assert first["extra"]["prompt_cache"]["cause"] == "first_request"
    assert second["extra"]["prompt_cache"]["full_miss"] is False
    # Append-only history and still nothing cached: the provider dropped it.
    assert third["extra"]["prompt_cache"]["cause"] == "provider_miss"


def test_a_rewritten_message_is_named_as_the_cause() -> None:
    tracker = PromptPrefixTracker()
    one = [ModelMessage("system", "s"), ModelMessage("user", "u"), ModelMessage("assistant", "a")]
    tracker.classify(tracker.observe(one, _TOOLS), {"prompt_tokens": 4000, "cache_read_tokens": 100})
    two = [ModelMessage("system", "s"), ModelMessage("user", "u (clipped)"), ModelMessage("assistant", "a"), ModelMessage("user", "next")]
    result = tracker.classify(tracker.observe(two, _TOOLS), {"prompt_tokens": 4000, "cache_read_tokens": 0})
    assert result["cause"] == "history_rewrite"
    assert result["rewritten_at"] == 1


def test_a_changed_tool_table_is_named_as_the_cause() -> None:
    tracker = PromptPrefixTracker()
    messages = [ModelMessage("system", "s"), ModelMessage("user", "u")]
    tracker.classify(tracker.observe(messages, _TOOLS), {"prompt_tokens": 4000, "cache_read_tokens": 10})
    other_tools = _TOOLS + (ProviderTool("git_diff", "diff", {"type": "object", "properties": {}}),)
    result = tracker.classify(
        tracker.observe(messages + [ModelMessage("user", "x")], other_tools),
        {"prompt_tokens": 4000, "cache_read_tokens": 0},
    )
    assert result["cause"] == "tools_changed"


def test_a_route_that_never_reports_cached_tokens_is_not_counted_as_missing() -> None:
    tracker = PromptPrefixTracker()
    messages = [ModelMessage("system", "s"), ModelMessage("user", "u")]
    tracker.classify(tracker.observe(messages, _TOOLS), {"prompt_tokens": 4000})
    result = tracker.classify(
        tracker.observe(messages + [ModelMessage("user", "x")], _TOOLS), {"prompt_tokens": 4100}
    )
    assert result["full_miss"] is None
    assert result["cause"] == "unreported"


def test_small_prompts_are_never_misses() -> None:
    tracker = PromptPrefixTracker()
    result = tracker.classify(
        tracker.observe([ModelMessage("user", "u")], _TOOLS), {"prompt_tokens": 300}
    )
    assert result["full_miss"] is False


def test_summary_counts_full_misses_by_cause() -> None:
    history = [
        {"role": "assistant", "prompt_cache": {"prompt_tokens": 5000, "cached_tokens": 0, "full_miss": True, "cause": "first_request"}},
        {"role": "tool", "content": "x"},
        {"role": "assistant", "prompt_cache": {"prompt_tokens": 6000, "cached_tokens": 5000, "full_miss": False}},
        {"role": "assistant", "prompt_cache": {"prompt_tokens": 7000, "cached_tokens": 0, "full_miss": True, "cause": "history_rewrite"}},
    ]
    summary = summarize(history)
    assert summary["requests"] == 3
    assert summary["full_misses"] == 2
    assert summary["causes"] == {"first_request": 1, "history_rewrite": 1}
    assert summary["cached_share"] == round(5000 / 18000, 4)


class _StreamContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *args: object) -> None:
        self.response.close()


class _Client:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        body = (
            "data: "
            + json.dumps({"id": "r", "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
            + "\n\ndata: [DONE]\n\n"
        )
        return _StreamContext(
            httpx.Response(
                200,
                request=httpx.Request("POST", url),
                content=body.encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        )


def _sent_payload(endpoint: str) -> Dict[str, Any]:
    from unittest.mock import patch

    client = _Client()
    provider = OpenAIChatCompletionsProvider(endpoint)
    request = ModelRequest(
        model="m",
        messages=(ModelMessage("user", "hi"),),
        deadline_seconds=5,
        cache_key="karox-abc-123",
    )
    with patch("karox.providers.httpx.Client", return_value=client):
        provider.complete(request)
    return client.calls[0]["json"]


def test_chat_completions_sends_the_key_only_to_openai() -> None:
    assert _sent_payload("https://api.openai.com/v1")["prompt_cache_key"] == "karox-abc-123"
    other = _sent_payload("https://api.deepseek.com/v1")
    assert "prompt_cache_key" not in other and other["user"] == "karox-abc-123"  # sticky routing elsewhere


def test_responses_sends_the_key_only_to_openai() -> None:
    request = ModelRequest(
        model="m", messages=(ModelMessage("user", "hi"),), deadline_seconds=5, cache_key="karox-k"
    )
    official = OpenAIResponsesProvider("https://api.openai.com/v1")._request_payload(request)
    gateway = OpenAIResponsesProvider("https://gateway.example/v1")._request_payload(request)
    assert official["prompt_cache_key"] == "karox-k"
    assert "prompt_cache_key" not in gateway


def test_anthropic_gets_cache_breakpoints_from_the_engine_key() -> None:
    request = ModelRequest(
        model="claude-x",
        messages=(ModelMessage("system", "sys"), ModelMessage("user", "hi")),
        deadline_seconds=5,
        cache_key="karox-k",
    )
    payload = AnthropicMessagesProvider("https://api.anthropic.com")._request_payload(request)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Mission 13: KV-Cache Preservation & Unified History-Management Regression
# ---------------------------------------------------------------------------


def _make_kernel_for_budget(
    tmp_path: Any,
    *,
    context: Any,
    max_output_tokens: Any = None,
    responses: Any = None,
    output_char_limit: int = 50_000,
) -> tuple[Any, Any, Any, Any]:
    import subprocess
    from pathlib import Path
    from karox.agent import AgentLimits, SYSTEM_PROMPT
    from karox.core_tools import ExtendedCoreRuntime
    from karox.engine_kernel import EngineKernel
    from karox.models import AccessProfile, Capability, Origin, OriginKind
    from karox.policy import CapabilityPolicy
    from karox.sessions import SessionStore

    repo = Path(tmp_path) / "repo"
    if not repo.exists():
        repo.mkdir(parents=True)
        (repo / "big.py").write_text(
            "value = 1\n" + ("# padding line\n" * 1_800) + "END = True\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    store_dir = Path(tmp_path) / "sessions"
    store = SessionStore(store_dir)
    origin = Origin(OriginKind.NATIVE_AGENT, "test-m13")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    grants = {
        Capability.REPO_READ,
        Capability.REPO_WRITE,
        Capability.CHECKS_RUN,
        Capability.PROCESS_RUN,
        Capability.GIT_READ,
    }
    policy.set_grants(origin, grants)
    check = ("python", "-c", "print('ok')")
    core = ExtendedCoreRuntime(
        repo,
        policy,
        store,
        Path(tmp_path) / "audit.jsonl",
        verification_commands=(check,),
    )

    class _Scripted:
        provider_name = "scripted"

        def __init__(self, items: List[ModelResponse]) -> None:
            self.items = list(items)
            self.requests: List[ModelRequest] = []

        def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if not self.items:
                return ModelResponse(
                    content=None,
                    tool_calls=(ToolCall("f-end", "finish", json.dumps({"answer": "done"})),),
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 1000, "completion_tokens": 10, "cache_read_tokens": 800},
                )
            return self.items.pop(0)

    provider = _Scripted(list(responses or []))
    kernel = EngineKernel(
        provider=provider,
        model="deepseek-v4.1-flash",
        core=core,
        sessions=store,
        origin=origin,
        limits=AgentLimits(max_steps=20, max_seconds=120.0),
        system_prompt=SYSTEM_PROMPT,
        context=context,
        max_output_tokens=max_output_tokens,
        output_char_limit=output_char_limit,
    )
    return kernel, provider, store, repo


def _cached_tool_response(
    calls: tuple[ToolCall, ...], *, prompt_tokens: int, cached_tokens: int = 4_000
) -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=calls,
        finish_reason="tool_calls",
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 20,
            "cache_read_tokens": cached_tokens,
        },
    )


def test_context_window_budgets_and_rewrite_pressure_across_windows(tmp_path: Any) -> None:
    """Scenarios 5, 6, 7, 8: 128K, 256K, 1M, and unknown context windows."""
    from karox.agent_contract import ContextBudget

    # Scenario 8: Unknown context window falls back conservatively (<= 96K).
    unknown = ContextBudget(max_input_tokens=None, window_source="fallback")
    k_unknown, _, _, _ = _make_kernel_for_budget(tmp_path / "u", context=unknown)
    assert unknown.window_known is False
    assert unknown.token_ceiling == 72_000
    assert k_unknown._engine_context_budget() == 54_000
    assert k_unknown._rewrite_pressure_tokens() == 27_000
    assert k_unknown._rewrite_pressure_tokens() <= 96_000

    # Scenario 5: 128K context window stays bounded below 96K and below engine_budget.
    win_128k = ContextBudget(max_input_tokens=128_000, utilization=0.8, window_source="advertised")
    k_128k, _, _, _ = _make_kernel_for_budget(
        tmp_path / "w128", context=win_128k, max_output_tokens=16_000
    )
    assert win_128k.window_known is True
    assert win_128k.token_ceiling == 102_400
    assert k_128k._engine_context_budget() == 64_800
    assert k_128k._rewrite_pressure_tokens() == 32_400
    assert k_128k._rewrite_pressure_tokens() < k_128k._engine_context_budget()

    # Scenario 6: 256K context window scales with usable budget while keeping headroom.
    win_256k = ContextBudget(max_input_tokens=256_000, utilization=0.8, window_source="advertised")
    k_256k, _, _, _ = _make_kernel_for_budget(
        tmp_path / "w256", context=win_256k, max_output_tokens=32_000
    )
    assert win_256k.token_ceiling == 204_800
    assert k_256k._engine_context_budget() == 129_600
    assert k_256k._rewrite_pressure_tokens() == 97_200
    assert k_256k._rewrite_pressure_tokens() < k_256k._engine_context_budget()

    # Scenario 7: 1M context window with 384K output reserve (Arena ultra config).
    win_1m = ContextBudget(max_input_tokens=1_000_000, utilization=0.8, window_source="advertised")
    k_1m, _, _, _ = _make_kernel_for_budget(
        tmp_path / "w1m", context=win_1m, max_output_tokens=384_000
    )
    assert win_1m.token_ceiling == 800_000
    assert k_1m._engine_context_budget() == 312_000
    assert k_1m._rewrite_pressure_tokens() == 234_000
    assert k_1m._rewrite_pressure_tokens() < k_1m._engine_context_budget()

    # 1M context window with lower utilization (0.6) and 384K output reserve adapts
    # downward instead of using 234K as a fixed constant (which would exceed 162K!).
    win_1m_med = ContextBudget(
        max_input_tokens=1_000_000, utilization=0.6, window_source="advertised"
    )
    k_1m_med, _, _, _ = _make_kernel_for_budget(
        tmp_path / "w1m_med", context=win_1m_med, max_output_tokens=384_000
    )
    assert k_1m_med._engine_context_budget() == 162_000
    assert k_1m_med._rewrite_pressure_tokens() == 121_500
    assert k_1m_med._rewrite_pressure_tokens() < k_1m_med._engine_context_budget()


def test_prompt_thresholds_below_96k_at_96k_150k_and_234k_on_1m_window(tmp_path: Any) -> None:
    """Scenarios 1, 2, 3, 4: <96K, ~96K, ~150K keep prefix intact; ~234K rewrites once."""
    from karox.agent_contract import ContextBudget

    win_1m = ContextBudget(max_input_tokens=1_000_000, utilization=0.8, window_source="advertised")
    big_write_1 = "value = 2\n" + ("# write1\n" * 3_500) + "END = True\n"
    big_write_2 = "value = 3\n" + ("# write2\n" * 3_500) + "END = True\n"
    big_write_3 = "value = 4\n" + ("# write3\n" * 3_500) + "END = True\n"

    responses = [
        # Step 1 (prompt=60K, <96K): read big.py (27KB).
        _cached_tool_response(
            (ToolCall("c1", "repo_read_file", json.dumps({"path": "big.py"})),),
            prompt_tokens=60_000,
        ),
        # Step 2 (prompt=60K -> returns 97K, ~96K): overwrite big.py (w1, >28KB).
        # Now c1 read is superseded (>24KB reclaimable), and prompt is 97K (~96K).
        _cached_tool_response(
            (
                ToolCall(
                    "c2",
                    "repo_write_file",
                    json.dumps({"path": "big.py", "content": big_write_1}),
                ),
            ),
            prompt_tokens=97_000,
        ),
        # Step 3 (sent at 97K -> must NOT rewrite; returns 150K, ~150K): overwrite big.py (w2).
        _cached_tool_response(
            (
                ToolCall(
                    "c3",
                    "repo_write_file",
                    json.dumps({"path": "big.py", "content": big_write_2}),
                ),
            ),
            prompt_tokens=150_000,
        ),
        # Step 4 (sent at 150K -> must NOT rewrite; returns 235K, ~234K): overwrite big.py (w3).
        # Now w1 (c2) is older than the 2 kept writes (c3, c4) AND c1 read is stale!
        _cached_tool_response(
            (
                ToolCall(
                    "c4",
                    "repo_write_file",
                    json.dumps({"path": "big.py", "content": big_write_3}),
                ),
            ),
            prompt_tokens=235_000,
        ),
        # Step 5 (sent after 235K -> unified rewrite MUST fire for BOTH c1 read AND c2 write):
        _cached_tool_response(
            (
                ToolCall(
                    "c5",
                    "checks_run",
                    json.dumps({"argv": ["python", "-c", "print('ok')"]}),
                ),
            ),
            prompt_tokens=180_000,
        ),
        # Step 6: finish.
        _cached_tool_response(
            (ToolCall("c6", "finish", json.dumps({"answer": "updated big.py"})),),
            prompt_tokens=181_000,
        ),
    ]

    kernel, provider, store, _ = _make_kernel_for_budget(
        tmp_path, context=win_1m, max_output_tokens=384_000, responses=responses
    )
    from karox.models import AccessProfile

    session = store.create(kernel.core.repository, "Update big.py", AccessProfile.WORKSPACE_WRITE)
    report = kernel.run(session.session_id)
    assert report.verified is True, report.reason

    def _tool_output(req_idx: int, call_id: str) -> str:
        for msg in provider.requests[req_idx].messages:
            if msg.role == "tool" and msg.tool_call_id == call_id:
                return msg.content or ""
        return ""

    def _call_args(req_idx: int, call_id: str) -> str:
        for msg in provider.requests[req_idx].messages:
            for call in msg.tool_calls:
                if call.call_id == call_id:
                    return call.raw_arguments
        return ""

    # Request 1 (after 60K, <96K): c1 read is untouched.
    assert "predates a later edit" not in _tool_output(1, "c1")
    # Request 2 (after 97K, ~96K): c1 read is already superseded by c2 write,
    # but at 97K on a 1M window it MUST NOT be rewritten!
    assert "predates a later edit" not in _tool_output(2, "c1")
    assert "# padding line" in _tool_output(2, "c1")
    # Request 3 (after 150K, ~150K): still below 234K pressure -> NO rewrite!
    assert "predates a later edit" not in _tool_output(3, "c1")
    assert "# write1" in _call_args(3, "c2")
    # Request 4 (after 235K, >=234K): BOTH stale read c1 AND old write c2 are
    # rewritten together in the SAME request (single coordinated cache break),
    # while the 2 most recent writes (c3, c4) stay intact!
    assert "predates a later edit" in _tool_output(4, "c1")
    assert "_karo_context_manager" in json.loads(_call_args(4, "c2"))
    assert "# write2" in _call_args(4, "c3")
    assert "# write3" in _call_args(4, "c4")
    # Request 5 (subsequent turn at 180K): prefix stays stable, no second rewrite!
    assert _tool_output(5, "c1") == _tool_output(4, "c1")
    assert _call_args(5, "c2") == _call_args(4, "c2")


def test_coordinated_batch_combines_stale_reads_and_old_writes(tmp_path: Any) -> None:
    """Unified policy: 14K stale read + 14K old write (<24K individually, 28K together) rewrite in one batch."""
    from karox.agent_contract import ContextBudget

    win_128k = ContextBudget(max_input_tokens=128_000, utilization=0.8, window_source="advertised")
    kernel, provider, store, repo = _make_kernel_for_budget(
        tmp_path, context=win_128k, max_output_tokens=16_000
    )
    # Make big.py ~15KB (>2K, <24K alone).
    (repo / "big.py").write_text(
        "value = 1\n" + ("# pad\n" * 2_500) + "END = True\n",
        encoding="utf-8",
    )
    w1_body = "value = 2\n" + ("# w1\n" * 2_800) + "END = True\n"
    w2_body = "value = 3\n" + ("# w2\n" * 2_800) + "END = True\n"
    w3_body = "value = 4\n" + ("# w3\n" * 2_800) + "END = True\n"

    provider.items = [
        _cached_tool_response(
            (ToolCall("c1", "repo_read_file", json.dumps({"path": "big.py"})),),
            prompt_tokens=10_000,
        ),
        _cached_tool_response(
            (ToolCall("c2", "repo_write_file", json.dumps({"path": "big.py", "content": w1_body})),),
            prompt_tokens=18_000,
        ),
        _cached_tool_response(
            (ToolCall("c3", "repo_write_file", json.dumps({"path": "big.py", "content": w2_body})),),
            prompt_tokens=25_000,
        ),
        # Crosses 128K pressure (32,400 tokens): c1 read (~14KB) + c2 write (~14KB) = ~28KB >= 24KB!
        _cached_tool_response(
            (ToolCall("c4", "repo_write_file", json.dumps({"path": "big.py", "content": w3_body})),),
            prompt_tokens=40_000,
        ),
        _cached_tool_response(
            (ToolCall("c5", "finish", json.dumps({"answer": "done"})),),
            prompt_tokens=33_000,
        ),
    ]
    from karox.models import AccessProfile

    session = store.create(repo, "Update big.py", AccessProfile.WORKSPACE_WRITE)
    report = kernel.run(session.session_id)
    assert report.verified is True, report.reason

    last_req = provider.requests[-1]
    c1_out = next(m.content for m in last_req.messages if m.role == "tool" and m.tool_call_id == "c1")
    c2_args = next(
        c.raw_arguments for m in last_req.messages for c in m.tool_calls if c.call_id == "c2"
    )
    assert "predates a later edit" in (c1_out or "")
    assert "_karo_context_manager" in json.loads(c2_args)


def test_latest_file_state_semantics_preserved_across_read_edit_write_patch(tmp_path: Any) -> None:
    """Scenario 15: failed edits do not mark reads stale; reads after the last edit stay intact."""
    from karox.agent_contract import ContextBudget

    win_128k = ContextBudget(max_input_tokens=128_000, utilization=0.8, window_source="advertised")
    kernel, provider, store, repo = _make_kernel_for_budget(
        tmp_path, context=win_128k, max_output_tokens=16_000
    )
    (repo / "other.py").write_text("X = 1\n" + ("# other\n" * 4_500), encoding="utf-8")

    provider.items = [
        # 1. Read big.py and other.py.
        _cached_tool_response(
            (
                ToolCall("r_app_1", "repo_read_file", json.dumps({"path": "big.py"})),
                ToolCall("r_other", "repo_read_file", json.dumps({"path": "other.py"})),
            ),
            prompt_tokens=20_000,
        ),
        # 2. Failed edit on other.py (anchor does not exist) + valid edit on big.py.
        _cached_tool_response(
            (
                ToolCall(
                    "e_other_fail",
                    "repo_edit_file",
                    json.dumps(
                        {"path": "other.py", "old_string": "MISSING_ANCHOR", "new_string": "X = 2"}
                    ),
                ),
                ToolCall(
                    "e_app_ok",
                    "repo_edit_file",
                    json.dumps(
                        {"path": "big.py", "old_string": "value = 1", "new_string": "value = 99"}
                    ),
                ),
            ),
            prompt_tokens=30_000,
        ),
        # 3. Fresh read of big.py AFTER the edit, at high prompt tokens (45K >= 32.4K pressure).
        _cached_tool_response(
            (ToolCall("r_app_fresh", "repo_read_file", json.dumps({"path": "big.py"})),),
            prompt_tokens=45_000,
        ),
        _cached_tool_response(
            (ToolCall("fin", "finish", json.dumps({"answer": "done"})),),
            prompt_tokens=40_000,
        ),
    ]
    from karox.models import AccessProfile

    session = store.create(repo, "Edit big.py", AccessProfile.WORKSPACE_WRITE)
    report = kernel.run(session.session_id)
    assert report.verified is True, report.reason

    last_tools = {
        m.tool_call_id: (m.content or "")
        for m in provider.requests[-1].messages
        if m.role == "tool"
    }
    # Earlier read of big.py before the successful edit IS marked stale.
    assert "predates a later edit" in last_tools["r_app_1"]
    # Read of other.py whose only subsequent edit FAILED is NOT marked stale!
    assert "predates a later edit" not in last_tools["r_other"]
    assert "# other" in last_tools["r_other"]
    # Fresh read of big.py AFTER the edit is NEVER marked stale and has exact latest state!
    assert "predates a later edit" not in last_tools["r_app_fresh"]
    assert "value = 99" in last_tools["r_app_fresh"]
    # Edit anchors in e_app_ok are preserved verbatim.
    edit_calls = {
        c.call_id: c.raw_arguments
        for m in provider.requests[-1].messages
        for c in m.tool_calls
    }
    assert "value = 99" in edit_calls["e_app_ok"]


def test_large_observations_read_and_write_outputs_bounded_at_creation(tmp_path: Any) -> None:
    """Scenarios 12, 13, 14: large tool observations, repo_read_file, and repo_write_file outputs."""
    from karox.agent_contract import ContextBudget

    win_1m = ContextBudget(max_input_tokens=1_000_000, utilization=0.8, window_source="advertised")
    kernel, provider, store, repo = _make_kernel_for_budget(
        tmp_path, context=win_1m, max_output_tokens=384_000, output_char_limit=12_000
    )
    huge_content = "value = 7\n" + ("# huge line\n" * 4_000) + "TAIL_MARKER = 1\n"
    provider.items = [
        # Scenario 14: large repo_write_file creates a compact diff observation (<1000 chars), not a 48KB echo.
        _cached_tool_response(
            (
                ToolCall(
                    "w_huge",
                    "repo_write_file",
                    json.dumps({"path": "big.py", "content": huge_content}),
                ),
            ),
            prompt_tokens=20_000,
        ),
        # Scenario 13: large repo_read_file (>12_000 chars) is bounded at creation time with head and tail kept.
        _cached_tool_response(
            (ToolCall("r_huge", "repo_read_file", json.dumps({"path": "big.py"})),),
            prompt_tokens=25_000,
        ),
        # Scenario 12: large generic tool observation is bounded at creation time.
        _cached_tool_response(
            (ToolCall("g_diff", "git_diff", "{}"),),
            prompt_tokens=30_000,
        ),
        _cached_tool_response(
            (ToolCall("fin", "finish", json.dumps({"answer": "done"})),),
            prompt_tokens=32_000,
        ),
    ]
    from karox.models import AccessProfile

    session = store.create(repo, "Write and read huge big.py", AccessProfile.WORKSPACE_WRITE)
    report = kernel.run(session.session_id)
    assert report.verified is True, report.reason

    tools = {
        m.tool_call_id: (m.content or "")
        for m in provider.requests[-1].messages
        if m.role == "tool"
    }
    # Write observation is compact at creation time (< 800 chars).
    assert len(tools["w_huge"]) < 800
    assert "[repo.write_file big.py]" in tools["w_huge"]
    assert "48026 bytes" in tools["w_huge"]
    # Read observation is bounded by output_char_limit at creation time and preserves head + tail.
    assert len(tools["r_huge"]) <= 12_500
    assert "value = 7" in tools["r_huge"]
    assert "TAIL_MARKER = 1" in tools["r_huge"]
    assert "karo-core" in tools["r_huge"]
    # Generic tool observation (git.diff) is also bounded by output_char_limit at creation time.
    assert len(tools["g_diff"]) <= 12_500


def test_repeated_compaction_resume_and_continue_preserve_valid_history(tmp_path: Any) -> None:
    """Scenarios 9, 10, 11: repeated compaction, resume after compaction, and continue after compaction."""
    from karox.agent_contract import ContextBudget

    # Tight budget to force multiple compactions across steps.
    tight = ContextBudget(max_input_tokens=1_000, utilization=0.8, window_source="advertised")
    w_a = "value = 2\n" + ("# block_a\n" * 600)
    w_b = "value = 3\n" + ("# block_b\n" * 600)

    # Run 1: two steps that trigger repeated compaction, then stop on step limit so we can resume.
    kernel1, provider1, store, repo = _make_kernel_for_budget(
        tmp_path,
        context=tight,
        responses=[
            _cached_tool_response(
                (ToolCall("c1", "repo_write_file", json.dumps({"path": "big.py", "content": w_a})),),
                prompt_tokens=3_000,
            ),
            _cached_tool_response(
                (ToolCall("c2", "repo_read_file", json.dumps({"path": "big.py"})),),
                prompt_tokens=4_000,
            ),
            _cached_tool_response(
                (ToolCall("c3", "repo_write_file", json.dumps({"path": "big.py", "content": w_b})),),
                prompt_tokens=4_000,
            ),
            _cached_tool_response(
                (ToolCall("c4", "repo_read_file", json.dumps({"path": "big.py"})),),
                prompt_tokens=4_000,
            ),
        ],
    )
    from karox.models import AccessProfile

    kernel1.limits = kernel1.limits.__class__(max_steps=3, max_seconds=120.0)
    kernel1._engine_keep_recent_messages = 2
    session = store.create(
        repo, "1. Update big.py\n2. Keep value() >= 1", AccessProfile.WORKSPACE_WRITE
    )
    report1 = kernel1.run(session.session_id)
    assert report1.status == "stopped"
    assert report1.compaction is not None
    assert report1.compaction["tokens_after"] < report1.compaction["tokens_before"]

    # Scenario 9: repeated compaction never modifies messages[0] or messages[1]
    # and never stacks duplicate state briefings anywhere in the request.
    first_sys = provider1.requests[0].messages[0].content
    first_task = provider1.requests[0].messages[1].content
    for req in provider1.requests:
        assert req.messages[0].content == first_sys
        assert req.messages[1].content == first_task
        assert "[KaroX durable state" not in (req.messages[1].content or "")
        total_briefings = sum(
            (msg.content or "").count("[KaroX durable state") for msg in req.messages
        )
        assert total_briefings <= 1

    # Scenario 10: Resume the compacted session and finish verified.
    kernel2, provider2, _, _ = _make_kernel_for_budget(
        tmp_path,
        context=tight,
        responses=[
            _cached_tool_response(
                (ToolCall("c5", "finish", json.dumps({"answer": "resumed and finished"})),),
                prompt_tokens=2_000,
            ),
        ],
    )
    kernel2._engine_keep_recent_messages = 2
    report2 = kernel2.run(session.session_id)
    assert report2.verified is True, report2.reason
    for req in provider2.requests:
        assert "[KaroX durable state" not in (req.messages[1].content or "")
        total_briefings = sum(
            (msg.content or "").count("[KaroX durable state") for msg in req.messages
        )
        assert total_briefings <= 1

    # Scenario 11: Continue task after compaction and verify.
    store.continue_task(session.session_id, "Now set value to 5")
    w_c = "value = 5\n"
    kernel3, provider3, _, _ = _make_kernel_for_budget(
        tmp_path,
        context=tight,
        responses=[
            _cached_tool_response(
                (ToolCall("c6", "repo_write_file", json.dumps({"path": "big.py", "content": w_c})),),
                prompt_tokens=2_500,
            ),
            _cached_tool_response(
                (ToolCall("c7", "finish", json.dumps({"answer": "continued and updated to 5"})),),
                prompt_tokens=2_500,
            ),
        ],
    )
    kernel3._engine_keep_recent_messages = 2
    report3 = kernel3.run(session.session_id)
    assert report3.verified is True, report3.reason
    assert (repo / "big.py").read_text(encoding="utf-8") == w_c


def test_state_briefing_preserves_prefix_and_advances_compaction_boundary(tmp_path: Any) -> None:
    """Mission 13.1 P0: messages[0] and messages[1] stay untouched; earliest change is >= 2 and advances."""
    from karox.agent_contract import ContextBudget
    from karox.models import AccessProfile
    from karox.state_briefing import HEADER

    tight = ContextBudget(max_input_tokens=6_000, utilization=0.8, window_source="advertised")
    # Budget is int(6000 * 0.8 * 0.75) = 3600 tokens (~14400 chars; system+task is ~2400 tokens).
    chunk_1 = "value = 10\n" + ("# step_chunk_1\n" * 450)
    chunk_2 = "value = 20\n" + ("# step_chunk_2\n" * 450)
    chunk_3 = "value = 30\n" + ("# step_chunk_3\n" * 450)
    # Include literal HEADER text inside a file to prove unrelated tool content is never stripped.
    marker_echo = f"# Keep literal header in code: {HEADER}\nvalue = 40\n"

    kernel, provider, store, repo = _make_kernel_for_budget(
        tmp_path,
        context=tight,
        responses=[
            _cached_tool_response(
                (ToolCall("c1", "repo_write_file", json.dumps({"path": "big.py", "content": chunk_1})),),
                prompt_tokens=2_600,
            ),
            _cached_tool_response(
                (ToolCall("c2", "repo_read_file", json.dumps({"path": "big.py"})),),
                prompt_tokens=3_700,
            ),
            # Compaction 1 triggers before step 3:
            _cached_tool_response(
                (ToolCall("c3", "repo_write_file", json.dumps({"path": "big.py", "content": chunk_2})),),
                prompt_tokens=3_100,
            ),
            _cached_tool_response(
                (ToolCall("c4", "repo_write_file", json.dumps({"path": "big.py", "content": chunk_3})),),
                prompt_tokens=3_800,
            ),
            # Compaction 2 triggers before step 5:
            _cached_tool_response(
                (ToolCall("c5", "repo_write_file", json.dumps({"path": "big.py", "content": marker_echo})),),
                prompt_tokens=2_900,
            ),
            _cached_tool_response(
                (ToolCall("c6", "repo_read_file", json.dumps({"path": "big.py"})),),
                prompt_tokens=3_000,
            ),
            _cached_tool_response(
                (ToolCall("c7", "finish", json.dumps({"answer": "done"})),),
                prompt_tokens=3_100,
            ),
        ],
    )
    kernel._engine_keep_recent_messages = 2
    task = f"1. Update big.py\n2. Note literal marker {HEADER} in task"
    session = store.create(repo, task, AccessProfile.WORKSPACE_WRITE)
    report = kernel.run(session.session_id)
    assert report.verified is True, report.reason
    assert report.compaction is not None
    assert report.compaction["count"] >= 2

    def _earliest_diff(req_a: ModelRequest, req_b: ModelRequest) -> int:
        limit = min(len(req_a.messages), len(req_b.messages))
        for idx in range(limit):
            if req_a.messages[idx] != req_b.messages[idx]:
                return idx
        return limit

    base_sys = provider.requests[0].messages[0]
    base_task = provider.requests[0].messages[1]
    assert HEADER in (base_task.content or "")

    compacted_indices: List[int] = []
    for req_idx, req in enumerate(provider.requests):
        # messages[0] and messages[1] are byte-for-byte identical in every request!
        assert req.messages[0] == base_sys
        assert req.messages[1] == base_task
        # Any state briefing attached by compaction sits at index >= 2 and appears at most once.
        tail_briefings = [
            idx
            for idx in range(2, len(req.messages))
            if f"\n\n{HEADER}\n" in (req.messages[idx].content or "")
            or (req.messages[idx].content or "").startswith(f"{HEADER}\n")
        ]
        assert len(tail_briefings) <= 1, f"request {req_idx} has duplicate briefings at {tail_briefings}"
        if tail_briefings:
            compacted_indices.append(tail_briefings[0])
        if req_idx > 0:
            earliest = _earliest_diff(provider.requests[req_idx - 1], req)
            assert earliest >= 2, f"request {req_idx} mutated prefix at index {earliest}"

    assert compacted_indices, "expected at least one compacted request with a state briefing"
    assert compacted_indices[0] >= 2
    assert compacted_indices[-1] >= compacted_indices[0]
    # Unrelated tool output containing HEADER (from reading big.py in c6) is preserved intact.
    c6_out = next(
        m.content for m in provider.requests[-1].messages if m.role == "tool" and m.tool_call_id == "c6"
    )
    assert f"# Keep literal header in code: {HEADER}" in (c6_out or "")


def test_context_window_safety_guards_provider_divergence_and_dense_inputs(tmp_path: Any) -> None:
    """Mission 13.1 P0: provider last_prompt_tokens divergence, Cyrillic/UTF-8, JSON escapes, and tool schemas."""
    from karox.agent_contract import ContextBudget
    from karox.models import AccessProfile
    from karox.providers import ProviderTool

    # 1. Synthetic provider reports last_prompt_tokens (62,000) much higher than chars//4 (~4,000)
    # on a 128K window (budget = 64,800). Adding a 4,000-token turn must trigger compaction BEFORE
    # the next request is sent!
    win_128k = ContextBudget(max_input_tokens=128_000, utilization=0.8, window_source="advertised")
    big_read_text = "value = 1\n" + ("# ascii line\n" * 1_200)
    kernel, provider, store, repo = _make_kernel_for_budget(
        tmp_path / "diverge",
        context=win_128k,
        max_output_tokens=16_000,
        responses=[
            _cached_tool_response(
                (ToolCall("c1", "repo_read_file", json.dumps({"path": "big.py"})),),
                prompt_tokens=62_000,
                cached_tokens=50_000,
            ),
            _cached_tool_response(
                (
                    ToolCall(
                        "c2",
                        "repo_write_file",
                        json.dumps({"path": "big.py", "content": big_read_text + "\nX = 2\n"}),
                    ),
                ),
                prompt_tokens=64_500,
                cached_tokens=55_000,
            ),
            _cached_tool_response(
                (ToolCall("c3", "repo_read_file", json.dumps({"path": "big.py"})),),
                prompt_tokens=45_000,
                cached_tokens=40_000,
            ),
            _cached_tool_response(
                (ToolCall("c4", "finish", json.dumps({"answer": "done"})),),
                prompt_tokens=46_000,
                cached_tokens=44_000,
            ),
        ],
    )
    kernel._engine_keep_recent_messages = 2
    session = store.create(repo, "Update big.py safely", AccessProfile.WORKSPACE_WRITE)
    report = kernel.run(session.session_id)
    assert report.verified is True, report.reason
    # Even though raw chars//4 was < 15,000 tokens, provider-reported 64,500 + delta exceeded
    # the 64,800 budget and triggered stale-read/compaction protection before request 3!
    c1_in_req2 = next(
        m.content for m in provider.requests[2].messages if m.role == "tool" and m.tool_call_id == "c1"
    )
    assert any(
        marker in (c1_in_req2 or "")
        for marker in ("predates a later edit", "clipped", "dropped by the karo-core context manager")
    )

    # 2. Dense Cyrillic/UTF-8, JSON escapes, and long tool schemas increase _estimate_tokens()
    # conservatively compared to plain ASCII of the same character length.
    from karox.engine_kernel import EngineAgent

    extra_tools = tuple(
        ProviderTool(
            f"custom_tool_{i}",
            "D" * 1_500,
            {"type": "object", "properties": {"payload": {"type": "string", "description": "S" * 1_500}}},
        )
        for i in range(12)
    )
    model_dense = EngineProviderModel(
        provider=provider,
        model_id="deepseek-v4.1-flash",
        tools=extra_tools,
        rewrite_pressure_tokens=27_000,
    )

    class _DummyEnv:
        def execute(self, action: Any) -> Dict[str, Any]:
            return {"output": "", "returncode": 0}

    agent_ascii = EngineAgent(
        EngineProviderModel(provider=provider, model_id="deepseek-v4.1-flash"),
        _DummyEnv(),
        kernel=kernel,
        context_token_budget=54_000,
        system_template="{{ system_prompt }}",
        instance_template="{{ task }}",
    )
    agent_dense = EngineAgent(
        model_dense,
        _DummyEnv(),
        kernel=kernel,
        context_token_budget=54_000,
        system_template="{{ system_prompt }}",
        instance_template="{{ task }}",
    )
    ascii_payload = "a" * 10_000
    cyrillic_and_escaped = ("ПроверкаКириллицы" * 250) + ('\\"\\n\\\\' * 900)
    assert len(cyrillic_and_escaped) <= len(ascii_payload)

    agent_ascii.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": ascii_payload},
    ]
    agent_dense.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": cyrillic_and_escaped},
    ]
    assert agent_dense._estimate_tokens() > agent_ascii._estimate_tokens() * 2


def test_early_high_reclaim_policy_rewrites_huge_dead_weight_once_without_oscillation(
    tmp_path: Any,
) -> None:
    """Mission 13.1 P1: small reclaim waits for 234K; huge reclaim (>=96K chars & >= suffix) rewrites once."""
    from karox.agent_contract import ContextBudget
    from karox.models import AccessProfile

    win_1m = ContextBudget(max_input_tokens=1_000_000, utilization=0.8, window_source="advertised")
    # 3 large writes (~52KB each) + 1 large stale read (~27KB) -> after 4th write,
    # 2 older writes (~104KB) + 1 stale read (~26KB) = ~130KB >= 96KB HIGH_RECLAIM_BATCH_CHARS,
    # while the kept suffix (last 2 writes ~104KB) is smaller than 130KB!
    huge_w1 = "value = 11\n" + ("# write_one_body\n" * 3_100)
    huge_w2 = "value = 12\n" + ("# write_two_body\n" * 3_100)
    huge_w3 = "value = 13\n" + ("# write_three_body\n" * 1_500)
    huge_w4 = "value = 14\n" + ("# write_four_body\n" * 1_500)

    responses = [
        _cached_tool_response(
            (ToolCall("r1", "repo_read_file", json.dumps({"path": "big.py"})),),
            prompt_tokens=20_000,
        ),
        _cached_tool_response(
            (ToolCall("w1", "repo_write_file", json.dumps({"path": "big.py", "content": huge_w1})),),
            prompt_tokens=40_000,
        ),
        # After w1, only r1 is stale (~26KB < 96KB) -> must NOT rewrite early!
        _cached_tool_response(
            (ToolCall("w2", "repo_write_file", json.dumps({"path": "big.py", "content": huge_w2})),),
            prompt_tokens=60_000,
        ),
        # After w2, w1 and w2 are the 2 kept writes; only r1 is stale (~26KB < 96KB) -> NO rewrite!
        _cached_tool_response(
            (ToolCall("w3", "repo_write_file", json.dumps({"path": "big.py", "content": huge_w3})),),
            prompt_tokens=75_000,
        ),
        # After w3, r1 (~26KB) + w1 (~52KB) = ~78KB (< 96KB) -> still NO early rewrite!
        _cached_tool_response(
            (ToolCall("w4", "repo_write_file", json.dumps({"path": "big.py", "content": huge_w4})),),
            prompt_tokens=95_000,
        ),
        # Now after w4: r1 (~26KB) + w1 (~52KB) + w2 (~52KB) = ~130KB >= 96KB,
        # and invalidated suffix (w3 + w4 ~ 51KB) < 130KB!
        # Must trigger ONE coordinated rewrite before Step 6 (even though 95K < 234K)!
        _cached_tool_response(
            (ToolCall("chk", "checks_run", json.dumps({"argv": ["python", "-c", "print('ok')"]})),),
            prompt_tokens=55_000,
        ),
        # Step 7: no oscillation / no second rewrite!
        _cached_tool_response(
            (ToolCall("fin", "finish", json.dumps({"answer": "done"})),),
            prompt_tokens=56_000,
        ),
    ]

    kernel, provider, store, repo = _make_kernel_for_budget(
        tmp_path, context=win_1m, max_output_tokens=384_000, responses=responses
    )
    session = store.create(repo, "Iterative rewrites on big.py", AccessProfile.WORKSPACE_WRITE)
    report = kernel.run(session.session_id)
    assert report.verified is True, report.reason

    def _tool_out(req_idx: int, cid: str) -> str:
        return next(
            (m.content or "" for m in provider.requests[req_idx].messages if m.role == "tool" and m.tool_call_id == cid),
            "",
        )

    def _call_raw(req_idx: int, cid: str) -> str:
        return next(
            (c.raw_arguments for m in provider.requests[req_idx].messages for c in m.tool_calls if c.call_id == cid),
            "",
        )

    # Requests 2, 3, 4 (small/moderate reclaim < 96KB): NO early rewrite!
    assert "predates a later edit" not in _tool_out(2, "r1")
    assert "predates a later edit" not in _tool_out(3, "r1")
    assert "predates a later edit" not in _tool_out(4, "r1")
    assert "# write_one_body" in _call_raw(4, "w1")

    # Request 5 (huge reclaim ~130KB >= 96KB and >= suffix): ONE coordinated rewrite of r1, w1, w2!
    assert "predates a later edit" in _tool_out(5, "r1")
    assert "_karo_context_manager" in json.loads(_call_raw(5, "w1"))
    assert "_karo_context_manager" in json.loads(_call_raw(5, "w2"))
    # Recent 2 writes (w3, w4) stay intact!
    assert "# write_three_body" in _call_raw(5, "w3")
    assert "# write_four_body" in _call_raw(5, "w4")

    # Request 6 (following turn): zero oscillation, identical prefix messages!
    assert provider.requests[6].messages[:10] == provider.requests[5].messages[:10]
