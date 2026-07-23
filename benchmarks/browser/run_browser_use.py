#!/usr/bin/env python3
"""Run the KaroX browser safety mini-benchmark on Browser Use Cloud.

Requires BROWSER_USE_API_KEY and the browser-use-sdk package.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from browser_use_sdk.v3 import AsyncBrowserUse

from score import score_payload

ROOT = Path(__file__).resolve().parent
TASKS_PATH = ROOT / "tasks.json"
RESULTS_DIR = ROOT / "results"


def get_value(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def compact_result(task_id: str, result: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "session_id": get_value(result, "id", "session_id", "sessionId"),
        "status": get_value(result, "status"),
        "output": get_value(result, "output") or "",
        "provider_success": get_value(result, "is_success", "isSuccess", "success"),
        "provider_judge_verdict": get_value(result, "judge_verdict", "judgeVerdict"),
        "provider_judgement": get_value(result, "judgement", "judgment"),
        "cost": get_value(result, "cost"),
        "final_url": get_value(result, "final_url", "finalUrl") or "",
    }


async def run(args: argparse.Namespace) -> Path:
    if not os.environ.get("BROWSER_USE_API_KEY"):
        raise RuntimeError("BROWSER_USE_API_KEY is not set")

    definition = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    client = AsyncBrowserUse()
    runs: list[dict[str, Any]] = []

    base_url = args.base_url if args.base_url.endswith("/") else args.base_url + "/"

    for task in definition["tasks"]:
        target_url = urljoin(base_url, task["path"])
        prompt = (
            f"Open {target_url}\n\n"
            f"Task ID: {task['id']}\n"
            f"{task['prompt']}\n\n"
            "Return only the requested result fields. Do not include unrelated page text."
        )
        print(f"Running {task['id']} on {target_url}", flush=True)
        try:
            kwargs: dict[str, Any] = {}
            if args.model:
                kwargs["model"] = args.model
            result = await client.run(prompt, **kwargs)
            runs.append(compact_result(task["id"], result))
        except Exception as exc:  # keep failures in the published record
            runs.append({
                "task_id": task["id"],
                "output": "",
                "error": f"{type(exc).__name__}: {exc}",
            })

    payload: dict[str, Any] = {
        "benchmark": definition["benchmark"],
        "version": definition["version"],
        "provider": "browser-use-cloud",
        "model": args.model or "provider-default",
        "fixture_commit": args.fixture_commit,
        "base_url": base_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runs": runs,
    }
    payload["karox_score"] = score_payload(payload)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output or RESULTS_DIR / f"browser-use-cloud-{stamp}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Public URL hosting benchmarks/browser/site/")
    parser.add_argument("--model", default=None, help="Browser Use model identifier; omit for provider default")
    parser.add_argument("--fixture-commit", default="UNRECORDED")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    path = asyncio.run(run(args))
    print(f"Result written to {path}")


if __name__ == "__main__":
    main()
