"""Best-effort cleanup for managed processes owned by one KaroX session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .paths import runtime_dir
from .remote_tools import _kill_pid_tree, _pid_alive


def stop_session_processes(
    session_id: str,
    *,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    if not session_id or any(
        not (character.isalnum() or character in "._-")
        for character in session_id
    ):
        return {"stopped": [], "invalid": True}
    process_root = (
        root
        or runtime_dir() / "vnext" / "ellipsis" / "processes" / session_id
    ).resolve()
    stopped: list[str] = []
    for metadata_path in sorted(process_root.glob("*.json")):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            process_id = str(payload["process_id"])
            owner = str(payload["session_id"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if owner != session_id or pid <= 0:
            continue
        if _pid_alive(pid):
            _kill_pid_tree(pid)
        if not _pid_alive(pid):
            stopped.append(process_id)
    return {"stopped": stopped, "invalid": False}


def stop_session_browser(session_id: str) -> dict[str, Any]:
    """No-op browser teardown hook for the managed-process cleanup path.

    A hosted bridge's browser state lives in the bridge process itself (a
    ``BrowserSessionManager`` holds the Playwright context), so it dies with
    that process when :func:`_cleanup_hosted_runtimes` runs on bridge exit.
    This function exists so lease revocation on the remote path -- which
    already calls :func:`stop_session_processes` -- has a symmetric, named
    browser hook to call without importing the bridge-only browser module here
    (which would create an import cycle).  It reports that there is nothing
    out-of-process to stop; in-process browser cleanup is the bridge's job.
    """
    if not session_id or any(
        not (character.isalnum() or character in "._-")
        for character in session_id
    ):
        return {"stopped": False, "invalid": True}
    return {"stopped": True, "invalid": False, "note": "browser state is in-process"}

