from __future__ import annotations

import json

from _support import SRC  # noqa: F401

from karox import tui


def test_jobs_is_discoverable_and_session_scoped() -> None:
    assert "/jobs" in tui.DISCOVERABLE_COMMANDS
    assert tui._slash_to_argv("/jobs", "session-7", "/repo") == [
        "jobs",
        "--json",
        "--session-id",
        "session-7",
    ]


def test_jobs_inspection_is_compact_and_human_readable() -> None:
    payload = {
        "count": 1,
        "total_count": 1,
        "truncated": False,
        "recent": [
            {
                "job_id": "job-aaaaaaaaaaaaaaaaaaaa",
                "status": "running",
                "kind": "command",
                "command": "python",
                "duration_seconds": 3.25,
            }
        ],
    }
    rendered = tui._inspection_text("/jobs", 0, json.dumps(payload), "en")
    assert "Durable jobs: 1" in rendered
    assert "job-aaaaaaaaaaaaaaaaaaaa" in rendered
    assert "running" in rendered
    assert "python" in rendered
