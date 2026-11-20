import asyncio
import subprocess

from karox6.core.events import Notice, RunFinished
from karox6.core.review import Finding, verified
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn


def git(ws, *args):
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)


def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q")
    git(ws, "config", "user.email", "t@example.com")
    git(ws, "config", "user.name", "t")
    (ws / "base.txt").write_text("base\n", encoding="utf-8")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "base")
    return ws


def run(session, prompt):
    async def collect():
        return [e async for e in session.run(prompt)]

    return asyncio.run(collect())


CHECK = "echo 'RESULT: 1 (expected 2)'; echo junk > review_tmp.txt"


def test_review_sends_back_only_reproduced_findings_and_cleans_up(tmp_path):
    ws = repo(tmp_path)
    provider = ScriptedProvider(
        [
            Turn(calls=[("write", {"file_path": "f.py", "content": "def f():\n    return 1\n"})]),
            Turn(text="done"),
            # the reviewer (fresh session, same provider)
            Turn(calls=[("bash", {"command": CHECK})]),
            Turn(
                calls=[
                    (
                        "finding",
                        {
                            "requirement": "f() must return 2",
                            "problem": "returns 1",
                            "command": CHECK,
                            "observed": "RESULT: 1 (expected 2)",
                        },
                    ),
                    (
                        "finding",  # never ran: dropped
                        {
                            "requirement": "x",
                            "problem": "y",
                            "command": "python check.py",
                            "observed": "FAILED",
                        },
                    ),
                ]
            ),
            Turn(text="checked 1 requirement, 2 findings"),
            # the agent again
            Turn(calls=[("write", {"file_path": "f.py", "content": "def f():\n    return 2\n"})]),
            Turn(text="fixed"),
        ]
    )
    config = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", review=True)
    session = Session(config, provider)
    events = run(session, "Make f() return 2.")
    finished = events[-1]
    assert isinstance(finished, RunFinished)
    assert finished.status == "completed"
    assert finished.final_text == "fixed"
    sent = [m for m in session.messages if m.meta.get("kind") == "review"]
    assert len(sent) == 1
    assert "f() must return 2" in sent[0].content
    assert "python check.py" not in sent[0].content
    assert not (ws / "review_tmp.txt").exists()  # the review's file was removed
    notes = [e.text for e in events if isinstance(e, Notice) and e.kind == "review"]
    assert any("2 reported, 1 reproduced" in n for n in notes)
    # the reviewer saw the task and the diff, not the agent's messages
    review_request = provider.requests[2]
    first = next(m for m in review_request.messages if m.role == "user")
    assert "Make f() return 2." in first.content
    assert "+    return 1" in first.content


def test_no_findings_finishes_at_once_and_the_review_runs_once(tmp_path):
    ws = repo(tmp_path)
    provider = ScriptedProvider(
        [
            Turn(calls=[("write", {"file_path": "f.py", "content": "x = 1\n"})]),
            Turn(text="done"),
            Turn(text="checked 3 requirements, 0 findings"),
        ]
    )
    config = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", review=True)
    events = run(Session(config, provider), "task")
    assert events[-1].final_text == "done"
    assert len(provider.requests) == 3


def test_review_is_skipped_without_changes(tmp_path):
    ws = repo(tmp_path)
    provider = ScriptedProvider([Turn(text="nothing to change")])
    config = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", review=True)
    run(Session(config, provider), "task")
    assert len(provider.requests) == 1


def test_verified_matches_command_and_output_loosely():
    item = Finding("r", "p", "pytest  -q  t.py", "assert 1 == 2\nsomething")
    assert verified([item], [("pytest -q t.py", "E   assert   1 == 2")])
    assert not verified([item], [("pytest -q t.py", "all passed")])
    assert not verified([item], [("pytest t.py", "assert 1 == 2")])
