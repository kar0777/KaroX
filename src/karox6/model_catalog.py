"""The open model catalog (models.dev): which models take pictures, how large their window is.

Providers rarely say in their model list whether a model accepts images, and families
change under the same name (DeepSeek Flash took text only, V4.1 Flash takes pictures).
models.dev is a community catalog of providers and their models (the one OpenCode uses):
for each provider its API address and, per model, the input kinds and limits.

KaroX downloads it at most once a day in the background, keeps a small digest of it in
the KaroX home (``model-catalog.json``) and answers from that digest:

- by server: the provider whose API host is the one KaroX talks to, exact model name;
- by name: "takes pictures" when nearly all providers serving a model of this name say
  so; never "no pictures" (some hosts serve a multimodal model text-only, and a wrong
  "no" would hide every picture, while unknown pictures are tried once and learned).

Off with ``model_catalog = false`` in config.toml or KAROX_MODEL_CATALOG=0.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

URL = "https://models.dev/api.json"
FILE_NAME = "model-catalog.json"
REFRESH_S = 24 * 3600  # a fresh digest is not downloaded again
RETRY_S = 3600  # after a failed download
MAX_BYTES = 64 * 1024 * 1024
FORMAT = 1

# Providers listed without an API address (their SDK knows it).
_KNOWN_HOSTS = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "google": "generativelanguage.googleapis.com",
    "mistral": "api.mistral.ai",
    "deepseek": "api.deepseek.com",
}


def name_key(model: str) -> str:
    """``openrouter/deepseek/deepseek-v4.1-flash:free`` -> ``deepseek-v4-1-flash``: the
    name without route prefixes or variant suffix, dots and underscores as dashes."""
    base = model.strip().lower().rsplit("/", 1)[-1].split(":", 1)[0]
    return base.replace(".", "-").replace("_", "-")


def host_of(url: str | None) -> str:
    try:
        return (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def _facts(model: Any) -> tuple[bool | None, int | None] | None:
    if not isinstance(model, dict):
        return None
    vision: bool | None = None
    modalities = model.get("modalities")
    if isinstance(modalities, dict) and isinstance(modalities.get("input"), list):
        vision = "image" in modalities["input"]
    window: int | None = None
    limit = model.get("limit")
    if isinstance(limit, dict):
        # what the model accepts as input: the input cap when there is one
        for field in ("input", "context"):
            value = limit.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**9:
                window = value
                break
    if vision is None and window is None:
        return None
    return vision, window


def digest(catalog: Any) -> dict[str, Any]:
    """The parts of models.dev KaroX uses: ``hosts`` (API host -> model -> facts) and
    ``names`` (model name -> whether it takes pictures when nearly all providers agree,
    else ``None``; the window most of them give)."""
    hosts: dict[str, dict[str, list[Any]]] = {}
    seen: dict[str, list[tuple[bool | None, int | None]]] = {}
    if not isinstance(catalog, dict):
        raise ValueError("not a catalog")
    for provider_id, provider in catalog.items():
        if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
            continue
        host = host_of(provider.get("api") if isinstance(provider.get("api"), str) else "")
        host = host or _KNOWN_HOSTS.get(str(provider_id), "")
        for model_id, model in provider["models"].items():
            found = _facts(model)
            if found is None:
                continue
            seen.setdefault(name_key(str(model_id)), []).append(found)  # each route votes
            if host:
                table = hosts.setdefault(host, {})
                key = str(model_id).strip().lower()
                if key in table and table[key] != list(found):  # two providers, one host
                    old = table[key]
                    found = (
                        old[0] if old[0] == found[0] else None,
                        min(w for w in (old[1], found[1]) if w) if old[1] or found[1] else None,
                    )
                table[key] = list(found)
    names: dict[str, list[Any]] = {}
    for key, all_facts in seen.items():
        visions = [v for v, _ in all_facts]  # a route that says nothing is not a "yes"
        windows = [w for _, w in all_facts if w]
        # "no pictures" is only believed from this server's own entry: a wrong "no"
        # blinds a model that sees, a wrong "unknown" costs one refused request (learned)
        vision = True if _consensus(visions) else None
        # routes cut windows differently: the one most of them give (the smaller on a tie)
        window = min(set(windows), key=lambda w: (-windows.count(w), w)) if windows else None
        if vision is not None or window is not None:
            names[key] = [vision, window]
    return {"format": FORMAT, "hosts": hosts, "names": names}


AGREE = 0.8  # share of the routes that must say the same about pictures


def _consensus(visions: list[bool | None]) -> bool | None:
    if not visions:
        return None
    if visions.count(True) >= AGREE * len(visions):
        return True
    if visions.count(False) >= AGREE * len(visions):
        return False
    return None


def _read(home: Path | None) -> dict[str, Any]:
    if home is None:
        return {}
    try:
        data = json.loads((home / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return {}
    return data


def lookup(home: Path | None, model: str, server: str | None = None) -> dict[str, Any]:
    """What the catalog says about ``model`` when served by ``server`` (vision, window)."""
    data = _read(home)
    if not data:
        return {}
    found: Any = None
    hosts = data.get("hosts")
    host = host_of(server)
    if host and isinstance(hosts, dict) and isinstance(hosts.get(host), dict):
        table = hosts[host]
        full = model.strip().lower()
        found = table.get(full)
        if found is None:  # the same server, the name spelt with another separator
            key = name_key(model)
            found = next((v for k, v in table.items() if name_key(k) == key), None)
    if found is None:
        names = data.get("names")
        found = names.get(name_key(model)) if isinstance(names, dict) else None
    if not isinstance(found, list) or len(found) != 2:
        return {}
    out: dict[str, Any] = {}
    if isinstance(found[0], bool):
        out["vision"] = found[0]
    if isinstance(found[1], int) and not isinstance(found[1], bool) and found[1] > 0:
        out["context_window"] = found[1]
    return out


def due(home: Path, now: float | None = None) -> bool:
    """Whether the digest should be downloaded again."""
    now = time.time() if now is None else now
    try:
        state = json.loads((home / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    fetched = state.get("fetched", 0) if state.get("format") == FORMAT else 0
    tried = state.get("tried", 0)
    fetched = fetched if isinstance(fetched, (int, float)) else 0
    tried = tried if isinstance(tried, (int, float)) else 0
    fresh = bool(fetched) and 0 <= now - fetched < REFRESH_S
    failed_lately = bool(tried) and 0 <= now - tried < RETRY_S
    return not fresh and not failed_lately


def _download(url: str) -> bytes:
    import httpx

    with (
        httpx.Client(timeout=30) as client,  # no redirects: a 3xx is a failed download
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError("catalog too large")
        return bytes(body)


def refresh(home: Path, *, url: str = URL, download: Any = None, now: float | None = None) -> bool:
    """Download the catalog when due and save its digest; True when a new one was saved.
    A failure is remembered for an hour (no download at every start while offline)."""
    now = time.time() if now is None else now
    if not due(home, now):
        return False
    try:
        raw = (download or _download)(url)
        data = digest(json.loads(raw))
    except Exception:  # offline, a bad answer: keep the old digest
        old = _read(home) or {"format": FORMAT}
        old["tried"] = now
        _write(home, old)
        return False
    data["fetched"] = now
    _write(home, data)
    return True


def _write(home: Path, data: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        home.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=home, prefix=".catalog-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
            os.replace(tmp, home / FILE_NAME)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
