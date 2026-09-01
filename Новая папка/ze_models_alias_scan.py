from __future__ import annotations

import ast
import json
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://ai.kscsnkli.site/v1/models"
SOURCE = Path(r"E:\Новая папка\test_ze_groups.py")
TIMEOUT = 8


def load_group_keys() -> dict[str, str]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "GROUP_KEYS":
                    value = ast.literal_eval(node.value)
                    if isinstance(value, dict):
                        return {str(k): str(v) for k, v in value.items()}
    raise RuntimeError("GROUP_KEYS not found")


def fetch_models(key: str) -> tuple[int | None, list[str]]:
    req = urllib.request.Request(
        BASE,
        headers={"Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": "ZE-Model-Alias-Scan/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(256000).decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, []
    except Exception:
        return None, []
    try:
        obj = json.loads(raw)
    except Exception:
        return status, []
    ids = []
    for item in obj.get("data") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
    return status, ids


def relevant(model_id: str) -> bool:
    low = model_id.lower()
    terms = ("claude", "opus", "sonnet", "fable", "1m", "thinking")
    return any(term in low for term in terms)


def main() -> int:
    result = {}
    for group, key in load_group_keys().items():
        status, models = fetch_models(key)
        result[group] = {"status": status, "models": [m for m in models if relevant(m)]}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
