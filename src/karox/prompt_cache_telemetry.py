"""Why a request missed the provider's prompt-prefix cache.

Every engine step resends the whole transcript. A provider that caches
prefixes bills the part it already holds at a cache-read rate, and a request
whose messages differ from the previous request at message ``k`` re-bills
everything from ``k`` on. A "full miss" -- a large prompt with zero cached
tokens -- costs the most, and the cause decides the fix:

* ``first_request``: nothing could be cached yet;
* ``tools_changed``: the advertised tool table differs from the last request;
* ``history_rewrite``: an earlier message was changed (elided arguments,
  clipped output, a different system prompt), so the prefix broke at ``k``;
* ``provider_miss``: the prefix was unchanged and the provider still had
  nothing (TTL expiry, eviction, routing to a cold replica).

The tracker hashes messages, never stores their text, so the persisted record
carries no content beyond what the transcript already holds.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: Providers do not cache prompts below roughly this size (OpenAI: 1024
#: tokens; Anthropic: 1024-4096 by model), so a zero there is no miss.
MIN_CACHEABLE_PROMPT_TOKENS = 1024


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def message_digest(message: Any) -> str:
    """A stable digest of everything about one message the wire sends."""

    calls = [
        (getattr(call, "call_id", ""), getattr(call, "name", ""), getattr(call, "raw_arguments", ""))
        for call in (getattr(message, "tool_calls", ()) or ())
    ]
    blocks = [
        (
            getattr(block, "kind", ""),
            getattr(block, "text", ""),
            getattr(block, "data", ""),
            getattr(block, "signature", ""),
        )
        for block in (getattr(message, "reasoning_blocks", ()) or ())
    ]
    images = []
    for image in getattr(message, "images", ()) or ():
        data = getattr(image, "data", b"")
        if isinstance(data, (bytes, bytearray)):
            images.append(hashlib.sha256(bytes(data)).hexdigest()[:16])
        else:
            images.append(_digest(repr(image)))
    return _digest(
        {
            "role": getattr(message, "role", ""),
            "content": getattr(message, "content", None),
            "tool_calls": calls,
            "tool_call_id": getattr(message, "tool_call_id", None),
            "reasoning_blocks": blocks,
            "reasoning_text": getattr(message, "reasoning_text", None),
            "images": images,
        }
    )


def tools_digest(tools: Iterable[Any]) -> str:
    return _digest(
        [
            (
                getattr(tool, "name", ""),
                getattr(tool, "description", ""),
                getattr(tool, "input_schema", {}),
            )
            for tool in tools
        ]
    )


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class PromptPrefixTracker:
    """Compares each request with the previous one and classifies its usage."""

    def __init__(self) -> None:
        self._previous: List[str] = []
        self._previous_tools: Optional[str] = None
        #: Set once any response reported a cached-token count above zero:
        #: before that a zero can mean "this route reports no cache at all".
        self.cache_reported = False

    def observe(self, messages: Sequence[Any], tools: Sequence[Any]) -> Dict[str, Any]:
        digests = [message_digest(message) for message in messages]
        tools_now = tools_digest(tools)
        stable = 0
        for before, now in zip(self._previous, digests):
            if before != now:
                break
            stable += 1
        observation: Dict[str, Any] = {
            "messages": len(digests),
            "previous_messages": len(self._previous),
            "stable_prefix_messages": stable,
            "first_request": not self._previous,
            "tools_changed": self._previous_tools is not None and self._previous_tools != tools_now,
            "rewritten_at": stable if stable < len(self._previous) else None,
        }
        self._previous = digests
        self._previous_tools = tools_now
        return observation

    def classify(self, observation: Mapping[str, Any], usage: Mapping[str, Any]) -> Dict[str, Any]:
        prompt = _int(usage.get("prompt_tokens", usage.get("input_tokens")))
        cached = _int(usage.get("cache_read_tokens"))
        if cached > 0:
            self.cache_reported = True
        result: Dict[str, Any] = {
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "stable_prefix_messages": int(observation.get("stable_prefix_messages") or 0),
            "messages": int(observation.get("messages") or 0),
        }
        if observation.get("rewritten_at") is not None:
            result["rewritten_at"] = int(observation["rewritten_at"])
        if prompt < MIN_CACHEABLE_PROMPT_TOKENS or cached > 0:
            result["full_miss"] = False
            return result
        if not self.cache_reported and not observation.get("first_request"):
            # This route has never reported a cached token: a zero is not
            # evidence of a miss, only of a provider that does not say.
            result["full_miss"] = None
            result["cause"] = "unreported"
            return result
        result["full_miss"] = True
        if observation.get("first_request"):
            result["cause"] = "first_request"
        elif observation.get("tools_changed"):
            result["cause"] = "tools_changed"
        elif observation.get("rewritten_at") is not None:
            result["cause"] = "history_rewrite"
        else:
            result["cause"] = "provider_miss"
        return result


def summarize(provider_history: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Full misses by cause across one session's assistant turns."""

    causes: Counter[str] = Counter()
    requests = 0
    prompt = 0
    cached = 0
    for entry in provider_history:
        if not isinstance(entry, Mapping) or entry.get("role") != "assistant":
            continue
        telemetry = entry.get("prompt_cache")
        if not isinstance(telemetry, Mapping):
            continue
        requests += 1
        prompt += _int(telemetry.get("prompt_tokens"))
        cached += _int(telemetry.get("cached_tokens"))
        if telemetry.get("full_miss") is True:
            causes[str(telemetry.get("cause") or "unknown")] += 1
    return {
        "requests": requests,
        "full_misses": sum(causes.values()),
        "causes": dict(causes),
        "cached_share": round(cached / prompt, 4) if prompt else None,
    }


__all__ = [
    "MIN_CACHEABLE_PROMPT_TOKENS",
    "PromptPrefixTracker",
    "message_digest",
    "summarize",
    "tools_digest",
]
