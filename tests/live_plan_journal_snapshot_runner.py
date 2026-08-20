from __future__ import annotations

import json
from pathlib import Path

import pytest

from _support import SRC  # noqa: F401
from karox.paths import session_dir

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_live_plan_journal_snapshot.json"
SESSION_ID = "web-saved-877046fafcba4c7dfb81c6b0"


def test_snapshot_live_plan_journals() -> None:
    plans = session_dir() / SESSION_ID / "plans"
    rows: list[dict[str, object]] = []
    if plans.is_dir():
        for path in sorted(plans.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)[:40]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            operations = payload.get("operations") if isinstance(payload, dict) else None
            rows.append(
                {
                    "file": path.name,
                    "status": payload.get("status") if isinstance(payload, dict) else None,
                    "started_at": payload.get("started_at") if isinstance(payload, dict) else None,
                    "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
                    "current_operation": payload.get("current_operation") if isinstance(payload, dict) else None,
                    "error_code": payload.get("error_code") if isinstance(payload, dict) else None,
                    "operations": {
                        str(key): value.get("status") if isinstance(value, dict) else None
                        for key, value in (operations.items() if isinstance(operations, dict) else [])
                    },
                }
            )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"schema_version": 1, "session_id": SESSION_ID, "plans": rows}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert plans.is_dir()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
