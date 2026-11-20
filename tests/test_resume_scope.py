from pathlib import Path

import pytest

from karox6.core.store import SessionStore, recent_sessions, resume_kwargs


def _session(home: Path, sid: str, workspace: Path) -> None:
    store = SessionStore(home, sid)
    store.append("meta", session_id=sid, workspace=str(workspace))
    store.append("message", message={"role": "user", "content": f"task in {workspace.name}"})


def test_resume_stays_in_its_project(tmp_path: Path) -> None:
    home, a, b = tmp_path / "home", tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _session(home, "s-a", a)
    _session(home, "s-b", b)  # the newest overall
    assert resume_kwargs(home, "last", workspace=a)["session_id"] == "s-a"
    assert [sid for sid, _ in recent_sessions(home, workspace=a)] == ["s-a"]
    with pytest.raises(ValueError, match="belongs to"):
        resume_kwargs(home, "s-b", workspace=a)
    with pytest.raises(ValueError, match="no session"):
        resume_kwargs(home, "last", workspace=tmp_path / "c")
    assert resume_kwargs(home, "s-b")["session_id"] == "s-b"  # no project given: as before
