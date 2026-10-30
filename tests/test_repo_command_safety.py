"""repo.command: no shell, honest refusals, and cleanup of the run's own files.

The arena run on ``swe-requests-6028`` sent 37 ``repo.command`` calls that all
failed: the model downloaded two reference files with ``web.fetch``, then tried
to remove them. ``web.fetch`` never recorded those files as created, so the
destructive gate treated the cleanup as deleting the user's source and asked
for a confirmation an unattended run cannot give. Every refusal named only the
field the model guessed wrong, so it kept inventing confirmation flags.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from karox.action_execution import CapabilityCoreRuntime
from karox.action_policy import ActionConfirmationRequired, ActionDecisionEngine
from karox import cli
from karox.core import CoreCommand, InvalidCommand
from karox.engine_tool_env import (
    BROWSER_CALLS_EXHAUSTED,
    BROWSER_CALLS_NOTICE,
    REFUSAL_DISABLE_AFTER,
    REFUSAL_GUIDANCE_AFTER,
    CoreToolEnvironment,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.risk_engine import RiskEngine
from karox.sessions import SessionStore

SOURCE_URL = "https://raw.githubusercontent.com/psf/requests/main/requests/utils.py"
FETCH_BODY = b"def prepend_scheme_if_needed(url, new_scheme):\n    return url\n"


class _FakeHeaders:
    def get_content_type(self) -> str:
        return "text/plain"

    def get(self, name: str) -> str | None:
        return str(len(FETCH_BODY)) if name == "Content-Length" else None


class _FakeResponse:
    def __init__(self) -> None:
        self.headers = _FakeHeaders()
        self.status = 200
        self._body = FETCH_BODY

    def geturl(self) -> str:
        return SOURCE_URL

    def read1(self, size: int) -> bytes:
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    def open(self, *args: object, **kwargs: object) -> _FakeResponse:
        return _FakeResponse()


def _git(repository: Path, *argv: str) -> None:
    subprocess.run(
        ["git", *argv],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def _runtime(tmp_path: Path, task: str) -> tuple[CapabilityCoreRuntime, SessionStore, Origin]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "add", "seed.txt")
    _git(repository, "commit", "-q", "-m", "seed")

    store = SessionStore(tmp_path / "sessions")
    store.create(
        repository,
        task,
        AccessProfile.WORKSPACE_WRITE,
        session_id="session-1",
    )
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    origin = Origin(OriginKind.NATIVE_AGENT, "repo-command-test")
    policy.set_grants(origin, cli._agent_grants(AccessProfile.WORKSPACE_WRITE, "build"))
    risk = RiskEngine()
    core = CapabilityCoreRuntime(
        repository,
        policy,
        store,
        tmp_path / "audit.jsonl",
        risk=risk,
        action_decisions=ActionDecisionEngine(risk),
    )
    return core, store, origin


def _command(
    name: str, arguments: dict, origin: Origin, *, key: str | None = None
) -> CoreCommand:
    return CoreCommand(
        name,
        arguments,
        "session-1",
        origin,
        idempotency_key=key,
        deadline_seconds=30.0,
    )


def _lease(store: SessionStore):
    return store.acquire("session-1", "test", ttl_seconds=30.0)


def test_repo_command_shell_shape_names_the_right_tool_and_runs_nothing(
    tmp_path: Path,
) -> None:
    core, store, origin = _runtime(tmp_path, "run the tests")
    marker = core.repository / "shell_ran.txt"
    lease = _lease(store)

    try:
        with pytest.raises(InvalidCommand) as refused:
            core.execute(
                _command(
                    "repo.command",
                    {
                        "action": "batch",
                        "payload": {"command": "echo ran > shell_ran.txt"},
                    },
                    origin,
                    key="repo-shell",
                ),
                lease=lease,
            )
    finally:
        store.release(lease)

    message = str(refused.value)
    assert "not a shell" in message
    for tool in ("checks.run", "tests.run", "dev.command"):
        assert tool in message
    assert not marker.exists(), "repo.command executed a shell"


def test_repo_command_action_without_the_batch_shape_is_explained(
    tmp_path: Path,
) -> None:
    core, _store, origin = _runtime(tmp_path, "delete scratch")

    # The JSON schema rejects an unknown action first, which is the path the
    # model actually took. The handler's own message has to explain the shape
    # for callers whose schema is lenient (MCP tools are validated again here).
    with pytest.raises(InvalidCommand, match="action must be one of"):
        core.execute(
            _command(
                "repo.command",
                {"action": "delete", "payload": {"path": "seed.txt"}},
                origin,
            )
        )

    with pytest.raises(InvalidCommand) as refused:
        core._prepare_repo_command(
            {"action": "delete", "payload": {"path": "seed.txt"}}, 30.0
        )

    message = str(refused.value)
    assert "apply_patch" in message and "batch" in message
    assert '"operations"' in message
    assert (core.repository / "seed.txt").exists()


def test_an_apply_patch_refusal_keeps_patch_guidance(
    tmp_path: Path,
) -> None:
    """A broken diff must not be answered with the batch JSON shape.

    In the 3d-viewer run a hunk-count error came back with "the only batch
    shape is ...", so the model retried the wrong form twice.
    """

    core, _store, _origin = _runtime(tmp_path, "fix the units conversion")

    with pytest.raises(InvalidCommand) as refused:
        core._prepare_apply_patch(
            {
                "patch": (
                    "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n one\n"
                )
            },
            30.0,
        )

    message = str(refused.value)
    assert "apply_patch takes one unified diff" in message
    assert "The only batch shape" not in message


def test_an_unknown_batch_field_lists_the_supported_fields() -> None:
    from karox import workspace_worker

    with pytest.raises(workspace_worker.InvalidCommand) as refused:
        workspace_worker.validate_repo_command(
            {"action": "batch", "payload": {"op": "delete", "path": "x"}}
        )

    message = str(refused.value)
    assert "unsupported fields: op, path" in message
    assert "supported fields: dry_run, operations" in message


def test_web_fetch_created_file_can_be_removed_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact benchmark loop: fetch a scratch copy, then delete it."""

    from karox import core_tools

    monkeypatch.setattr(core_tools, "build_opener", lambda *args, **kwargs: _FakeOpener())
    core, store, origin = _runtime(
        tmp_path, "read the reference file, then remove it"
    )
    lease = _lease(store)

    try:
        fetched = core.execute(
            _command(
                "web.fetch",
                {"url": SOURCE_URL, "path": "scratch_ref.py"},
                origin,
                key="fetch-1",
            ),
            lease=lease,
        )
        assert fetched.ok is True, fetched.data
        record = store.load("session-1")
        assert "scratch_ref.py" in record.created_files

        removed = core.execute(
            _command(
                "repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [{"op": "delete", "path": "scratch_ref.py"}]
                    },
                },
                origin,
                key="delete-1",
            ),
            lease=lease,
        )
    finally:
        store.release(lease)

    assert removed.ok is True, removed.data
    assert not (core.repository / "scratch_ref.py").exists()
    assert "scratch_ref.py" not in store.load("session-1").created_files


def test_a_file_the_session_did_not_create_still_needs_confirmation(
    tmp_path: Path,
) -> None:
    """The scratch exception must not become a general delete permission."""

    core, store, origin = _runtime(tmp_path, "delete the obsolete legacy file")
    core.repository.joinpath("legacy.py").write_text("old\n", encoding="utf-8")
    lease = _lease(store)

    try:
        with pytest.raises(ActionConfirmationRequired) as stopped:
            core.execute(
                _command(
                    "repo.command",
                    {
                        "action": "batch",
                        "payload": {
                            "operations": [{"op": "delete", "path": "legacy.py"}]
                        },
                    },
                    origin,
                    key="delete-legacy",
                ),
                lease=lease,
            )
    finally:
        store.release(lease)

    assert "may not confirm" in str(stopped.value)
    assert (core.repository / "legacy.py").exists()


def test_repeated_refusals_escalate_and_then_disable_one_tool() -> None:
    environment = object.__new__(CoreToolEnvironment)
    environment._refusals = {}

    first = environment._note_refusal("repo.command", "InvalidCommand")
    second = environment._note_refusal("repo.command", "ActionConfirmationRequired")
    assert first == 1 and second == REFUSAL_GUIDANCE_AFTER
    guidance = environment._refusal_guidance("InvalidCommand", second)
    assert "Do not repeat this shape" in guidance

    assert environment._disabled_after_refusals("repo.command") == ""
    count = second
    while count < REFUSAL_DISABLE_AFTER:
        # Alternating error types are still one guessing streak.
        count = environment._note_refusal("repo.command", "InvalidCommand")
    disabled = environment._disabled_after_refusals("repo.command")
    assert "disabled for the rest of this run" in disabled

    # A success from the same tool is progress and clears the history.
    environment._refusals.pop("repo.command", None)
    assert environment._disabled_after_refusals("repo.command") == ""


def test_only_a_code_change_resets_the_research_clock() -> None:
    from karox.engine_tool_env import _counts_as_repository_change

    assert _counts_as_repository_change("repo.edit_file", {"changed": True}, True)
    assert _counts_as_repository_change(
        "repo.command", {"changed_files": ["a.py"]}, True
    )
    # Downloading a reference file is research; it must not buy another budget.
    assert not _counts_as_repository_change("web.fetch", {"changed": True}, True)
    assert not _counts_as_repository_change("repo.edit_file", {"changed": False}, True)
    assert not _counts_as_repository_change(
        "repo.apply_patch", {"changed_files": ["a.py"]}, False
    )


def test_research_budget_speaks_twice_then_stays_quiet() -> None:
    from types import SimpleNamespace

    environment = object.__new__(CoreToolEnvironment)
    environment._last_mutation_step = 0
    environment._research_notices = set()
    environment._research_applies = True
    environment._kernel = SimpleNamespace(_step=11)
    read = SimpleNamespace(mutates=False)
    write = SimpleNamespace(mutates=True)

    assert environment._research_notice(read) == ""
    environment._kernel._step = 12
    first = environment._research_notice(read)
    assert "Research budget" in first
    assert environment._research_notice(read) == "", "the notice must not repeat"

    environment._kernel._step = 24
    second = environment._research_notice(read)
    assert "exhausted" in second
    assert environment._research_notice(read) == ""

    # A change resets the clock, and a mutation never carries the notice.
    environment._last_mutation_step = 24
    environment._research_notices = set()
    assert environment._research_notice(write) == ""
    environment._kernel._step = 30
    assert environment._research_notice(read) == ""

    # A read-only question is allowed to survey without being told to edit.
    environment._research_applies = False
    environment._kernel._step = 40
    assert environment._research_notice(read) == ""


def test_browser_budget_warns_once_then_disables_probing() -> None:
    """A browser is for checking a named interaction, not endless exploration.

    Two 3D re-runs used 57 and 61 browser calls, fixed the artifacts, then hit
    the wall clock without final proof. The notice arrives at 20 calls and the
    tool stops accepting calls at 36; visual.check and repository reads stay.
    """

    from types import SimpleNamespace

    environment = object.__new__(CoreToolEnvironment)
    environment._browser_calls = 0
    environment._browser_notice_sent = False
    environment._time_notice_sent = False
    environment._kernel = SimpleNamespace(remaining_seconds=lambda: 1000.0)

    assert environment._browser_notice() == ""
    assert environment._browser_budget_exhausted("browser.command") == ""

    environment._browser_calls = BROWSER_CALLS_NOTICE
    notice = environment._browser_notice()
    assert "Browser budget" in notice
    assert environment._browser_notice() == "", "the notice must not repeat"
    assert environment._browser_budget_exhausted("browser.command") == ""

    environment._browser_calls = BROWSER_CALLS_EXHAUSTED
    disabled = environment._browser_budget_exhausted("browser.command")
    assert "disabled" in disabled
    assert environment._browser_budget_exhausted("visual.check") == ""


def test_time_left_notice_fires_once_near_the_deadline() -> None:
    from types import SimpleNamespace

    environment = object.__new__(CoreToolEnvironment)
    environment._time_notice_sent = False
    environment._kernel = SimpleNamespace(remaining_seconds=lambda: 299.0)

    first = environment._time_left_notice()
    assert "Only 299 seconds remain" in first
    assert environment._time_left_notice() == ""

    later = object.__new__(CoreToolEnvironment)
    later._time_notice_sent = False
    later._kernel = SimpleNamespace(remaining_seconds=lambda: 900.0)
    assert later._time_left_notice() == ""


def test_web_fetch_does_not_claim_an_existing_file_was_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karox import core_tools

    monkeypatch.setattr(core_tools, "build_opener", lambda *args, **kwargs: _FakeOpener())
    core, store, origin = _runtime(tmp_path, "refresh a vendored copy")
    (core.repository / "scratch_ref.py").write_text("old copy\n", encoding="utf-8")
    lease = _lease(store)

    try:
        fetched = core.execute(
            _command(
                "web.fetch",
                {"url": SOURCE_URL, "path": "scratch_ref.py"},
                origin,
                key="fetch-1",
            ),
            lease=lease,
        )
    finally:
        store.release(lease)
    assert fetched.ok is True, fetched.data
    assert "scratch_ref.py" not in store.load("session-1").created_files