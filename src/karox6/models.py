"""What KaroX knows about a model: does it take pictures, how large is its window.

Four sources, the first that has a field wins:

1. ``models.json`` in the KaroX home, written by the user::

       {"models": {"zai-org/GLM-5.3-Flash": {"vision": false, "context_window": 200000},
                   "my-local-*": {"vision": true}}}

   Keys are model names or ``fnmatch`` patterns (case-insensitive).
2. What a session learned and saved there under ``"learned"`` (a provider that answered
   "this model does not support images" -> ``{"vision": false}``).
3. What the provider's model list said, saved there under ``"provider"`` per server
   when the list was read (Mistral, OpenRouter, Gemini... give the window and image
   input); it applies only to requests to that same server.
4. The table below: only facts that hold for every route serving the family. Unknown
   stays ``None``; callers treat that as "try it" for what the user sent and "do not"
   for what KaroX would add on its own (tool screenshots).
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

FILE_NAME = "models.json"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    vision: bool | None = None  # accepts image input
    context_window: int | None = None  # tokens the model accepts (None: unknown)
    source: str = "unknown"  # where it came from: user | learned | provider | built-in

    def describe(self) -> str:
        vision = {True: "yes", False: "no", None: "unknown"}[self.vision]
        window = window_text(self.context_window) if self.context_window else "unknown"
        return f"pictures: {vision} · window: {window} ({self.source})"


def window_text(tokens: int) -> str:
    """131072 -> "128k", 200000 -> "200k", 1048576 -> "1M"."""
    unit = 1024 if tokens % 1024 == 0 else 1000
    if tokens >= 1000 * unit:
        return f"{tokens / unit**2:.3g}M"
    return f"{round(tokens / unit)}k"


# (pattern on the lower-cased model name without a provider prefix, facts)
_BUILTIN: tuple[tuple[str, dict[str, Any]], ...] = (
    # Claude: every model since 3 takes pictures; before 4.6 the standard window is 200k
    # (1M only with a beta header), 4.6 and later are larger than KaroX's working limit.
    (
        r"claude-(?:3|3-\d)-|claude-(?:opus|sonnet|haiku)-4(?:-[0-5])?(?:$|-\d{6,}|-latest)",
        {"vision": True, "context_window": 200_000},
    ),
    (r"claude-", {"vision": True}),
    (r"(?:^|[-_/])(?:fable|mythos)", {"vision": True}),
    (r"^(?:gpt-4o|gpt-4\.1|gpt-4\.5|gpt-5|gpt-6|o3|o4|chatgpt-4o)", {"vision": True}),
    (r"^gpt-oss", {"vision": False}),
    (r"^gpt-3\.5", {"vision": False}),
    (r"^gemini", {"vision": True}),
    (r"(?:[-_.]vl(?:[-_.]|$)|vision|llava|pixtral|glm-\d(?:\.\d)?v)", {"vision": True}),
    # DeepSeek's own API takes text only (chat, reasoner, V3/V4 families).
    (r"^deepseek", {"vision": False}),
)


def _base_name(model: str) -> str:
    """``openrouter/deepseek/deepseek-v4.1`` -> ``deepseek-v4.1``; lower case."""
    return model.strip().lower().rsplit("/", 1)[-1].split(":", 1)[0]


def _builtin(model: str) -> dict[str, Any]:
    name = _base_name(model)
    found: dict[str, Any] = {}
    for pattern, facts in _BUILTIN:
        if re.search(pattern, name):
            for key, value in facts.items():
                found.setdefault(key, value)
    return found


def _read(home: Path | None) -> dict[str, Any]:
    if home is None:
        return {}
    try:
        data = json.loads((home / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _match(table: Any, model: str) -> dict[str, Any]:
    if not isinstance(table, dict):
        return {}
    full, base = model.strip().lower(), _base_name(model)
    exact = {str(k).lower(): v for k, v in table.items() if isinstance(v, dict)}
    for key in (full, base):
        if key in exact:
            return exact[key]
    for key, facts in exact.items():
        if fnmatch.fnmatchcase(full, key) or fnmatch.fnmatchcase(base, key):
            return facts
    return {}


def _exact(table: Any, model: str) -> dict[str, Any]:
    """The provider's facts are about exactly this name (no prefix stripping, no patterns:
    another route serving a similar name may differ)."""
    if not isinstance(table, dict):
        return {}
    facts = table.get(model.strip())
    return facts if isinstance(facts, dict) else {}


def _clean(facts: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(facts.get("vision"), bool):
        out["vision"] = facts["vision"]
    window = facts.get("context_window")
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        out["context_window"] = window
    return out


def server_key(base_url: str | None) -> str:
    """The server a provider's facts belong to: its URL without a trailing slash."""
    return (base_url or "").strip().rstrip("/").lower()


def model_info(model: str, home: Path | None = None, server: str | None = None) -> ModelInfo:
    data = _read(home)
    info = ModelInfo()
    listed: Any = {}
    if server:
        by_server = data.get("provider")
        listed = by_server.get(server_key(server)) if isinstance(by_server, dict) else {}
    # Lowest priority first: each later source overwrites what it knows.
    for source, found in (
        ("built-in", _builtin(model)),
        ("provider", _exact(listed, model)),
        ("learned", _match(data.get("learned"), model)),
        ("user", _match(data.get("models"), model)),
    ):
        facts = _clean(found)
        if facts:
            info = replace(info, **facts, source=source)
    return info


def remember(home: Path | None, model: str, **facts: Any) -> None:
    """Save what a session found out about ``model`` (best effort, atomic write)."""
    if home is None:
        return
    data = _read(home)
    stored = data.get("learned")
    learned: dict[str, Any] = stored if isinstance(stored, dict) else {}
    entry = dict(learned.get(model) or {})
    entry.update(_clean(facts))
    learned[model] = entry
    data["learned"] = learned
    _write(home, data)


def remember_listed(
    home: Path | None, server: str | None, listed: dict[str, dict[str, Any]]
) -> None:
    """Save what ``server``'s model list said (model -> facts); a model it says nothing
    about keeps what was saved before."""
    if home is None or not server:
        return
    found = {model: _clean(facts) for model, facts in listed.items()}
    found = {model: facts for model, facts in found.items() if facts}
    if not found:
        return
    data = _read(home)
    stored = data.get("provider")
    servers: dict[str, Any] = stored if isinstance(stored, dict) else {}
    old = servers.get(server_key(server))
    table: dict[str, Any] = dict(old) if isinstance(old, dict) else {}
    if all(table.get(m) == f for m, f in found.items()):
        return  # nothing new: no write
    table.update(found)
    servers[server_key(server)] = table
    data["provider"] = servers
    _write(home, data)


def _write(home: Path, data: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        home.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=home, prefix=".models-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1, ensure_ascii=False)
            os.replace(tmp, home / FILE_NAME)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


_NO_IMAGES = re.compile(
    r"image|vision|multimodal|multi-modal|image_url|content type|unsupported content",
    re.IGNORECASE,
)


def rejects_images(code: str, message: str) -> bool:
    """A provider error that says the model does not take pictures."""
    return code in ("bad_request", "unsupported", "invalid_request") and bool(
        _NO_IMAGES.search(message)
    )
