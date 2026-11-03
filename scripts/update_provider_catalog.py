"""Refresh the built-in provider catalog snapshot from models.dev.

    python scripts/update_provider_catalog.py            # download api.json
    python scripts/update_provider_catalog.py api.json   # use a local copy

The snapshot is the explicit update path for ``karox.provider_catalog``:
nothing is fetched at runtime. models.dev (https://github.com/sst/models.dev,
MIT licence) is the community catalog OpenCode reads; only the fields KaroX
uses are kept, and only models that can call tools and answer in text, because
those are the only ones an agent session can drive.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://models.dev/api.json"
TARGET = Path(__file__).resolve().parents[1] / "src" / "karox" / "data" / "models_dev.json.gz"


def _model_row(model: dict) -> list | None:
    if not model.get("tool_call"):
        return None
    modalities = model.get("modalities") or {}
    if "text" not in (modalities.get("output") or ["text"]):
        return None
    cost = model.get("cost") or {}
    limit = model.get("limit") or {}
    return [
        str(model.get("name") or ""),
        limit.get("context") or None,
        limit.get("output") or None,
        1 if "image" in (modalities.get("input") or []) else 0,
        1 if model.get("reasoning") else 0,
        cost.get("input"),
        cost.get("output"),
        cost.get("cache_read"),
        cost.get("cache_write"),
        str(model.get("release_date") or ""),
        str(model.get("status") or ""),
    ]


def build(payload: dict, fetched_at: str) -> dict:
    providers = {}
    for provider_id in sorted(payload):
        provider = payload[provider_id]
        models = {}
        for model_id in sorted(provider.get("models") or {}):
            row = _model_row(provider["models"][model_id])
            if row is not None:
                models[model_id] = row
        providers[provider_id] = {
            "name": provider.get("name") or provider_id,
            "npm": provider.get("npm") or "",
            "api": provider.get("api") or "",
            "env": list(provider.get("env") or []),
            "doc": provider.get("doc") or "",
            "models": models,
        }
    return {
        "schema": 1,
        "source": SOURCE,
        "license": "MIT (https://github.com/sst/models.dev)",
        "fetched_at": fetched_at,
        "model_columns": [
            "name", "context", "output", "vision", "reasoning", "input_per_million",
            "output_per_million", "cache_read_per_million", "cache_write_per_million",
            "release_date", "status",
        ],
        "providers": providers,
    }


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        raw = Path(argv[1]).read_bytes()
        fetched_at = _dt.date.fromtimestamp(Path(argv[1]).stat().st_mtime).isoformat()
    else:
        request = urllib.request.Request(SOURCE, headers={"User-Agent": "karox-catalog-update"})
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
            raw = response.read()
        fetched_at = _dt.date.today().isoformat()
    snapshot = build(json.loads(raw), fetched_at)
    data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the compressed bytes reproducible for the same catalog.
    TARGET.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    count = sum(len(item["models"]) for item in snapshot["providers"].values())
    print(f"{TARGET.name}: {len(snapshot['providers'])} providers, {count} models, {TARGET.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
