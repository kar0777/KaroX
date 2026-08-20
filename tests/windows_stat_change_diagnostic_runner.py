"""Explicit diagnostic for same-size/same-mtime rewrite metadata on this host."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stat_payload(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "birthtime_ns": getattr(stat, "st_birthtime_ns", 0),
    }


def test_same_size_same_mtime_rewrite_metadata() -> None:
    with tempfile.TemporaryDirectory(prefix="karox-stat-change-") as tmp:
        path = Path(tmp) / "value.txt"
        path.write_bytes(b"alpha\n")
        before = path.stat()
        before_payload = _stat_payload(path)
        path.write_bytes(b"omega\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after_payload = _stat_payload(path)
        payload = {
            "before": before_payload,
            "after": after_payload,
            "ctime_changed": before_payload["ctime_ns"] != after_payload["ctime_ns"],
            "birthtime_changed": before_payload["birthtime_ns"] != after_payload["birthtime_ns"],
        }
        output = ROOT / "benchmarks" / "agent_throughput" / "latest_windows_stat_change.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("WINDOWS_STAT_CHANGE=" + json.dumps(payload, sort_keys=True))
