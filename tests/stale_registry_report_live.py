"""Live, read-only report of stale project paths in the real saved-profile store.

Not collected by default discovery (the filename does not match ``test_*.py``).
Run explicitly:

    python -m pytest tests/stale_registry_report_live.py -s -q

Prints every saved profile's repository/projects and flags entries whose paths
no longer exist. Mutates nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path


def test_report_stale_saved_profile_projects() -> None:
    from karox.web_bridge_profiles import default_web_bridge_profile_path

    store_path = default_web_bridge_profile_path().expanduser()
    print(f"\nstore: {store_path} exists={store_path.exists()}")
    if not store_path.exists():
        return
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    for raw in payload.get("profiles", []):
        name = raw.get("name")
        default_id = raw.get("default_project_id")
        print(f"profile: {name!r} target={raw.get('target_profile')!r}")
        repo = raw.get("repository")
        repo_exists = Path(repo).exists() if repo else None
        marker = " <-- STALE" if repo_exists is False else ""
        print(f"  repository: {repo!r} exists={repo_exists}{marker}")
        for entry in raw.get("projects", []) or []:
            path = entry.get("path")
            exists = Path(path).exists() if path else None
            stale = " <-- STALE" if exists is False else ""
            is_default = " (default)" if entry.get("project_id") == default_id else ""
            print(
                f"  project {entry.get('project_id')!r} label={entry.get('label')!r}: "
                f"{path!r} exists={exists}{stale}{is_default}"
            )
