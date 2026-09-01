from pathlib import Path
import re

P = Path(r"E:\Новая папка\ze_anthropic_proxy.py")
s = P.read_text(encoding="utf-8")

s = s.replace(
'''CONTEXT_SOFT_LIMIT_TOKENS = 20000
CONTEXT_TARGET_TOKENS = 17000
CONTEXT_SUMMARY_RESERVE_TOKENS = 1400
CONTEXT_MIN_RECENT_TOKENS = 3500
CONTEXT_SUMMARY_CHUNK_CHARS = 18000
CONTEXT_SUMMARY_MAX_TOKENS = 1200
CONTEXT_MAX_SUMMARY_CALLS = 24''',
'''VIRTUAL_CONTEXT_WINDOW_TOKENS = 200000
CONTEXT_SOFT_LIMIT_TOKENS = 28000
CONTEXT_TARGET_TOKENS = 24000
CONTEXT_SUMMARY_RESERVE_TOKENS = 1800
CONTEXT_MIN_RECENT_TOKENS = 4500
CONTEXT_SUMMARY_CHUNK_CHARS = 52000
CONTEXT_SUMMARY_MAX_TOKENS = 1400
CONTEXT_MAX_SUMMARY_CALLS = 8
CONTEXT_ARCHIVE_MAX_CHARS = 46000
CONTEXT_EXACT_USER_TAIL = 6
TOOL_DESCRIPTION_LIMIT = 240''')

needle = '''def _json_token_estimate(value) -> int:
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        rendered = str(value)
    return max(1, len(rendered) // 4)
'''
insert = needle + r'''

_SCHEMA_DROP_KEYS = frozenset({
    "description", "title", "examples", "example", "default", "$comment",
    "deprecated", "readOnly", "writeOnly",
})


def _clip_middle(value: str, limit: int) -> str:
    value = str(value or "")
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    keep = max(180, (limit - 150) // 2)
    omitted = max(0, len(value) - keep * 2)
    return value[:keep] + f"\n...[locally compacted; omitted_chars={omitted}; sha256={digest}]...\n" + value[-keep:]


def _compact_schema(value):
    if isinstance(value, dict):
        return {k: _compact_schema(v) for k, v in value.items() if k not in _SCHEMA_DROP_KEYS}
    if isinstance(value, list):
        return [_compact_schema(v) for v in value]
    return value


def compact_tools_for_forwarding(tools) -> list:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        item = {"name": str(tool["name"])}
        description = str(tool.get("description") or "").strip()
        if description:
            item["description"] = _clip_middle(description, TOOL_DESCRIPTION_LIMIT)
        item["input_schema"] = _compact_schema(tool.get("input_schema") or {"type": "object", "properties": {}})
        out.append(item)
    return out


def _history_archive(messages: list, prior_summary: str = "") -> tuple[str, str]:
    users = []
    events = []
    user_messages = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if role == "user":
            text = content_to_text(content).strip()
            if text:
                user_messages.append(text)
                users.append(f"[USER REQUIREMENT {index}]\n{text}")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = str(block.get("tool_use_id") or "")
                        rendered = _clip_middle(content_to_text(block.get("content")), 900)
                        events.append(f"[TOOL RESULT {index} id={tid}]\n{rendered}")
        elif role == "assistant":
            text_parts = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool_use":
                        args = json.dumps(block.get("input") or {}, ensure_ascii=False, separators=(",", ":"))
                        events.append(
                            f"[TOOL USE {index}] name={block.get('name') or 'tool'} id={block.get('id') or ''} input={_clip_middle(args, 1200)}"
                        )
            text = "\n".join(x for x in text_parts if x).strip()
            if text:
                events.append(f"[ASSISTANT {index}]\n{_clip_middle(text, 1000)}")

    user_count = max(1, len(users))
    per_user = max(650, min(3000, 26000 // user_count))
    user_section = "\n\n".join(_clip_middle(x, per_user + 80) for x in users)
    event_section = "\n\n".join(events[-32:])
    archive_parts = []
    if prior_summary:
        archive_parts.append("[PREVIOUS ROLLING MEMORY]\n" + _clip_middle(prior_summary, 9000))
    if user_section:
        archive_parts.append("[USER REQUIREMENTS ACROSS OLDER TURNS]\n" + user_section)
    if event_section:
        archive_parts.append("[RECENT OLDER WORK EVENTS]\n" + event_section)
    archive = "\n\n".join(archive_parts)
    archive = _clip_middle(archive, CONTEXT_ARCHIVE_MAX_CHARS)

    exact_tail = "\n\n".join(
        f"[EXACT RECENT USER TEXT]\n{_clip_middle(text, 2600)}"
        for text in user_messages[-CONTEXT_EXACT_USER_TAIL:]
    )
    return archive, exact_tail
'''
if needle not in s:
    raise SystemExit("helper insertion marker missing")
s = s.replace(needle, insert, 1)

route_pattern = re.compile(r'''def route_key_for_request\(headers, body: dict\) -> str:\n.*?\n\n\ndef ordered_route_ids''', re.S)
route_repl = r'''def route_key_for_request(headers, body: dict) -> str:
    raw = headers.get("x-claude-code-session-id") or headers.get("X-Claude-Code-Session-Id")
    if not raw:
        metadata = body.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("user_id"), str):
            raw = metadata["user_id"]
    if not raw:
        first_user = ""
        for message in body.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "user":
                first_user = content_to_text(message.get("content"))
                if first_user:
                    break
        seed = system_text(body.get("system"))[:8000] + "\n" + first_user[:16000]
        raw = "conversation:" + hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()
    return hashlib.sha256(str(raw).encode("utf-8", errors="replace")).hexdigest()[:24]


def ordered_route_ids'''
s, n = route_pattern.subn(route_repl, s, count=1)
if n != 1:
    raise SystemExit("route_key replacement failed")

compact_pattern = re.compile(r'''def compact_context_body\(body: dict, route_key: str\) -> tuple\[dict, dict\]:\n.*?\n\n\ndef image_to_openai''', re.S)
compact_repl = r'''def compact_context_body(body: dict, route_key: str) -> tuple[dict, dict]:
    original_estimate = estimate_input_tokens(body)
    candidate = dict(body)
    original_tools = body.get("tools") or []
    candidate["tools"] = compact_tools_for_forwarding(original_tools)
    tool_compacted_estimate = estimate_input_tokens(candidate)
    meta = {
        "compacted": tool_compacted_estimate < original_estimate,
        "tools_compacted": bool(original_tools),
        "original_estimate": original_estimate,
        "forwarded_estimate": tool_compacted_estimate,
        "summarized_messages": 0,
        "summary_calls": 0,
        "cache_hit": False,
        "virtual_context_window": VIRTUAL_CONTEXT_WINDOW_TOKENS,
    }
    if tool_compacted_estimate <= CONTEXT_SOFT_LIMIT_TOKENS:
        return candidate, meta

    messages = candidate.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise ContextCompactionError(
            f"ZE virtual-context guard: estimated input {original_estimate} exceeds the safe forwarded window and no conversation history can be compacted"
        )

    fixed = dict(candidate)
    fixed["messages"] = []
    fixed_tokens = estimate_input_tokens(fixed)
    recent_budget = max(
        CONTEXT_MIN_RECENT_TOKENS,
        CONTEXT_TARGET_TOKENS - fixed_tokens - CONTEXT_SUMMARY_RESERVE_TOKENS,
    )
    start = _select_recent_start(messages, recent_budget)

    # The active/current user turn is never summarized. If a fresh turn alone is
    # too large, preserving it is more important than pretending it fits.
    if start <= 0:
        candidate["messages"] = _shrink_recent_tool_results(messages, per_result_chars=2600)
        forwarded = estimate_input_tokens(candidate)
        if forwarded > CONTEXT_SOFT_LIMIT_TOKENS:
            raise ContextCompactionError(
                f"ZE virtual-context guard: current turn estimates {forwarded} tokens after tool-schema compaction; active user text is kept verbatim rather than silently truncated"
            )
        meta.update({"compacted": True, "forwarded_estimate": forwarded})
        return candidate, meta

    state = _load_context_state(route_key)
    prior_count = int(state.get("summarized_message_count") or 0)
    prior_summary = str(state.get("summary") or "")
    reusable = (
        prior_count <= start
        and prior_count > 0
        and prior_summary
        and state.get("prefix_hash") == _messages_prefix_hash(messages, prior_count)
    )

    if reusable and prior_count == start:
        memory = prior_summary
        exact_tail = str(state.get("exact_tail") or "")
        summary_calls = 0
        cache_hit = True
    else:
        delta_start = prior_count if reusable else 0
        archive, exact_tail = _history_archive(messages[delta_start:start], prior_summary if reusable else "")
        if not archive.strip():
            raise ContextCompactionError("virtual context selected an empty historical prefix")
        # One bounded ZE call is enough because the local archive has already
        # removed bulk tool noise while retaining all user requirements and the
        # most recent concrete work events.
        if len(archive) > 14000:
            memory = _summary_call(archive, route_key, 1)
            summary_calls = 1
        else:
            memory = archive
            summary_calls = 0
        memory = _clip_middle(memory, 9000)
        cache_hit = False
        _save_context_state(route_key, {
            "schema_version": 2,
            "summarized_message_count": start,
            "prefix_hash": _messages_prefix_hash(messages, start),
            "summary": memory,
            "exact_tail": exact_tail,
            "updated_at": time.time(),
        })

    continuity = (
        "[LOCAL 200K CONTEXT MEMORY]\n"
        "This is a rolling continuation memory derived from older turns of this same session. "
        "Use it as prior state, not as a new request. The current user turn below is authoritative and verbatim.\n\n"
        + memory
    )
    if exact_tail:
        continuity += "\n\n[RECENT USER REQUIREMENTS KEPT NEAR-VERBATIM]\n" + _clip_middle(exact_tail, 12000)
    summary_message = {"role": "user", "content": continuity}

    recent = _shrink_recent_tool_results(messages[start:], per_result_chars=2600)
    candidate["messages"] = [summary_message, *recent]
    forwarded = estimate_input_tokens(candidate)

    if forwarded > CONTEXT_SOFT_LIMIT_TOKENS:
        # Keep the same exact current turn, but shrink bulky tool results harder.
        recent = _shrink_recent_tool_results(messages[start:], per_result_chars=1100)
        summary_message["content"] = _clip_middle(summary_message["content"], 9000)
        candidate["messages"] = [summary_message, *recent]
        forwarded = estimate_input_tokens(candidate)

    if forwarded > CONTEXT_SOFT_LIMIT_TOKENS:
        raise ContextCompactionError(
            f"ZE virtual-context guard could reduce {original_estimate} to {forwarded} tokens, still above safe forwarded limit {CONTEXT_SOFT_LIMIT_TOKENS}; current user turn was not altered"
        )

    meta.update({
        "compacted": True,
        "forwarded_estimate": forwarded,
        "summarized_messages": start,
        "summary_calls": summary_calls,
        "cache_hit": cache_hit,
    })
    return candidate, meta


def image_to_openai'''
s, n = compact_pattern.subn(compact_repl, s, count=1)
if n != 1:
    raise SystemExit("compact_context_body replacement failed")

s = s.replace('server_version = "ZEAnthropicProxy/2.9"', 'server_version = "ZEAnthropicProxy/3.0"')
s = s.replace('"adapter_version": "2.9"', '"adapter_version": "3.0"')
s = s.replace('user_agent: str = "ZE-Claude-Proxy/2.6"', 'user_agent: str = "ZE-Claude-Proxy/3.0"')
s = s.replace('user_agent=f"ZE-Claude-Proxy/2.6 route-{attempt}"', 'user_agent=f"ZE-Claude-Proxy/3.0 route-{attempt}"')
s = s.replace('"route_pool_size": len(load_route_ids()),\n            })', '"route_pool_size": len(load_route_ids()),\n                "virtual_context_window": VIRTUAL_CONTEXT_WINDOW_TOKENS,\n            })', 1)

P.write_text(s, encoding="utf-8")
print("patched", P)
