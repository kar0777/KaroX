"""Find model servers that already run on this machine.

``karox provider local`` probes the default loopback ports of Ollama, LM
Studio, llama.cpp's server, vLLM and SGLang with ``GET /v1/models`` -- the
OpenAI-compatible listing all five serve -- and prints the ready
``karox provider setup local-openai ...`` command for each find.

* Only an OpenAI-shaped model list counts as a find. Ports 8000 and 8080 are
  just as common for unrelated dev servers, which answer with HTML or 404 and
  are skipped rather than reported as a model server.
* The runtime is named from the listing's ``owned_by`` when that identifies
  one (llama.cpp answers ``llamacpp`` even on another port) and from the port
  otherwise; ``identified_by`` says which, so a guess is never presented as a
  fact.
* Proxy variables are ignored (``trust_env=False``): with ``HTTPS_PROXY`` set
  and no ``NO_PROXY`` entry for localhost the probe would otherwise go to the
  proxy, which cannot reach this machine's loopback.

Nothing is installed or written; the caller decides what to connect.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 0.8
_MAX_LISTING_BYTES = 2 * 1024 * 1024
_MAX_MODELS = 200


@dataclass(frozen=True)
class RuntimeKind:
    runtime_id: str
    display_name: str
    port: int
    owned_by: Tuple[str, ...]


KNOWN_RUNTIMES: Tuple[RuntimeKind, ...] = (
    RuntimeKind("ollama", "Ollama", 11434, ("library", "ollama")),
    RuntimeKind("lm-studio", "LM Studio", 1234, ("organization_owner", "lmstudio", "lm-studio")),
    RuntimeKind("llama-cpp", "llama.cpp server", 8080, ("llamacpp", "llama.cpp")),
    RuntimeKind("vllm", "vLLM", 8000, ("vllm",)),
    RuntimeKind("sglang", "SGLang", 30000, ("sglang",)),
)


@dataclass(frozen=True)
class LocalRuntime:
    runtime_id: str
    display_name: str
    base_url: str
    models: Tuple[str, ...]
    identified_by: str

    def setup_argv(self, model_id: Optional[str] = None, program: str = "karox") -> Tuple[str, ...]:
        model = model_id or (self.models[0] if self.models else "<model>")
        return (
            program,
            "provider",
            "setup",
            "local-openai",
            "--provider-id",
            self.runtime_id,
            "--base-url",
            self.base_url,
            "--model",
            model,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "models": list(self.models),
            "identified_by": self.identified_by,
            "setup_argv": list(self.setup_argv()),
        }


def _listing_models(payload: Any) -> Optional[List[dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    entries = [
        item
        for item in payload["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
    ]
    if payload["data"] and not entries:
        return None
    return entries[:_MAX_MODELS]


def _identify(kind: RuntimeKind, entries: Sequence[dict[str, Any]], kinds: Iterable[RuntimeKind]) -> Tuple[RuntimeKind, str]:
    owners = {str(item.get("owned_by") or "").strip().lower() for item in entries}
    owners.discard("")
    for candidate in kinds:
        if owners & set(candidate.owned_by):
            return candidate, "owned_by"
    return kind, "port"


def probe_runtime(
    kind: RuntimeKind,
    *,
    host: str = DEFAULT_HOST,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    kinds: Sequence[RuntimeKind] = KNOWN_RUNTIMES,
) -> Optional[LocalRuntime]:
    base_url = f"http://{host}:{kind.port}/v1"
    try:
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 0.4)),
        ) as client:
            response = client.get(base_url + "/models", headers={"Accept": "application/json"})
    except (httpx.HTTPError, OSError):
        return None
    if response.status_code != 200 or len(response.content) > _MAX_LISTING_BYTES:
        return None
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    entries = _listing_models(payload)
    if entries is None:
        return None
    identified, how = _identify(kind, entries, kinds)
    models = tuple(dict.fromkeys(item["id"].strip() for item in entries))
    return LocalRuntime(identified.runtime_id, identified.display_name, base_url, models, how)


def scan_local_runtimes(
    *,
    host: str = DEFAULT_HOST,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    kinds: Sequence[RuntimeKind] = KNOWN_RUNTIMES,
) -> List[LocalRuntime]:
    """Every known runtime that answers on its port, in ``kinds`` order."""

    if not kinds:
        return []
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        found = list(
            pool.map(lambda kind: probe_runtime(kind, host=host, timeout=timeout, kinds=kinds), kinds)
        )
    runtimes: List[LocalRuntime] = []
    taken: set[str] = set()
    for runtime in found:
        if runtime is None:
            continue
        runtime_id = runtime.runtime_id
        if runtime_id in taken:
            # Two servers identified as the same runtime (say two llama.cpp
            # instances): keep both, under distinct registry ids.
            port = runtime.base_url.rsplit(":", 1)[-1].split("/", 1)[0]
            runtime_id = f"{runtime_id}-{port}"
            runtime = LocalRuntime(runtime_id, runtime.display_name, runtime.base_url, runtime.models, runtime.identified_by)
        taken.add(runtime_id)
        runtimes.append(runtime)
    return runtimes


__all__ = [
    "DEFAULT_HOST",
    "KNOWN_RUNTIMES",
    "LocalRuntime",
    "RuntimeKind",
    "probe_runtime",
    "scan_local_runtimes",
]
