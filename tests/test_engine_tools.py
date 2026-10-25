"""The integrator's engine wiring: browser and desktop as typed Core tools.

These tests pin the B7 contract: ``browser.command`` and the ``desktop.*``
tools are registered around the engine the default ``karox agent run`` uses,
flow through the same capability policy and risk gate, and are advertised to
the model under their provider aliases. Elevated-only surfaces stay hidden
from ordinary write sessions, and a direct call is refused, not executed.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Any, List

import pytest

from karox.models import AccessProfile
from karox.providers import ModelResponse
from karox.risk_mapping import risk_kind_for

from test_engine_kernel import (
    ScriptedProvider,
    _agent_run_args,
    _cli_repo,
    _finish_call,
    _parse_stream,
    _request_text,
    _request_tools,
    _response,
    _tool_call,
    _write_call,
)


def _browser_call(call_id: str, action: str, payload: dict[str, Any]) -> Any:
    return _tool_call(call_id, "browser_command", {"action": action, "payload": payload})


def _desktop_call(call_id: str, action: str, payload: dict[str, Any]) -> Any:
    return _tool_call(call_id, f"desktop_{action.split('.', 1)[1]}", dict(payload))


def _scripted_cli(monkeypatch: pytest.MonkeyPatch, responses: List[ModelResponse]) -> Any:
    from karox import cli

    provider = ScriptedProvider(responses)
    monkeypatch.setattr(cli, "OpenAIChatCompletionsProvider", lambda *args, **kwargs: provider)
    return provider


def _elevated_repo(tmp_path: Path) -> Path:
    from karox.paths import runtime_dir
    from karox.sessions import SessionStore

    repository = _cli_repo(tmp_path)
    store = SessionStore(runtime_dir() / "vnext" / "sessions")
    store.create(
        repository,
        "create hello.txt with hi",
        AccessProfile.ELEVATED,
        session_id="engine-1",
    )
    return repository


@pytest.fixture()
def fixture_site() -> Any:
    from _browser_site import site_server

    with site_server() as url:
        yield url


def test_risk_kinds_cover_the_desktop_surface() -> None:
    """Every desktop tool has an explicit risk kind; none fall to unknown-high."""

    read_actions = (
        "desktop.status",
        "desktop.list_windows",
        "desktop.list_apps",
        "desktop.request_approval",
        "desktop.attach",
        "desktop.detach",
        "desktop.capture",
        "desktop.inspect_ui",
    )
    for action in read_actions:
        assert risk_kind_for(action, {}) == "desktop.read", action
    for action in ("desktop.type_text", "desktop.press_key", "desktop.click", "desktop.scroll"):
        assert risk_kind_for(action, {}) == "desktop.input", action
    for action in ("desktop.launch_app", "desktop.open_terminal", "desktop.show_window"):
        assert risk_kind_for(action, {}) == "desktop.launch", action


def test_risk_kinds_grade_the_read_shaped_browser_actions() -> None:
    """The read-shaped browser actions no longer fall back to browser.input."""

    for action in ("cookies", "page_info", "recover"):
        assert risk_kind_for("browser.command", {"action": action}) == "browser.read", action
    for action in ("back", "forward", "reload", "clear", "dblclick", "focus", "type"):
        assert (
            risk_kind_for("browser.command", {"action": action}) == "browser.input"
        ), action


def test_cli_engine_run_advertises_and_executes_browser_command(
    fixture_site: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default engine run drives a real Chromium through browser.command."""

    from karox.browser_bootstrap import playwright_chromium_status
    from karox import cli

    if not playwright_chromium_status().ready:
        pytest.skip("managed chromium is not provisioned")

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = _cli_repo(tmp_path)
    provider = _scripted_cli(
        monkeypatch,
        [
            _response(
                (_browser_call("c1", "open", {"url": fixture_site}),),
                content="opening the fixture site",
            ),
            _response(
                (_browser_call("c2", "get_text", {"selector": "#intro"}),),
                content="reading the page",
            ),
            _response((_write_call("c3", "hello.txt", "hi"),), content="writing the file"),
            _response((_finish_call("c4", "created hello.txt with hi"),)),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(_agent_run_args(repository))
    assert code == 0
    # The stable browser.command surface is advertised under its provider alias.
    assert "browser_command" in _request_tools(provider.requests[0])
    # The real page's text rode back into the model's history as the observation.
    assert "Welcome to the local fixture site." in _request_text(provider.requests[2])
    _, report = _parse_stream(output.getvalue())
    assert report["verified"] is True
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_cli_engine_run_advertises_desktop_tools_for_elevated_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Elevated runs see the desktop tools; desktop.status really executes."""

    from karox import cli

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = _elevated_repo(tmp_path)
    provider = _scripted_cli(
        monkeypatch,
        [
            _response((_desktop_call("c1", "desktop.status", {}),), content="checking desktop"),
            _response((_write_call("c2", "hello.txt", "hi"),), content="writing the file"),
            _response((_finish_call("c3", "created hello.txt with hi"),)),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(_agent_run_args(repository))
    assert code == 0
    advertised = _request_tools(provider.requests[0])
    assert "desktop_status" in advertised
    assert "desktop_list_windows" in advertised
    assert "desktop_open_terminal" in advertised
    # desktop.status really ran: the control contract rode back into history.
    assert "win32-window-messages" in _request_text(provider.requests[1])
    _, report = _parse_stream(output.getvalue())
    assert report["verified"] is True
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_cli_engine_run_hides_desktop_tools_from_write_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace_write run never advertises desktop tools, and a direct call
    is refused as a policy denial observation instead of executing."""

    from karox import cli

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = _cli_repo(tmp_path)
    provider = _scripted_cli(
        monkeypatch,
        [
            _response((_desktop_call("c1", "desktop.status", {}),), content="trying desktop"),
            _response((_write_call("c2", "hello.txt", "hi"),), content="writing the file"),
            _response((_finish_call("c3", "created hello.txt with hi"),)),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(_agent_run_args(repository))
    assert code == 0
    advertised = _request_tools(provider.requests[0])
    assert "desktop_status" not in advertised
    assert "browser_command" in advertised
    # The refused call came back as a denial observation, not a contract report.
    assert "denied" in _request_text(provider.requests[1]).lower()
    _, report = _parse_stream(output.getvalue())
    assert report["verified"] is True


class _HostedRuntime:
    """The shape a hosted bridge runtime presents to the worker."""

    session_id = "hosted-session-1"
    hosted_origin: Any = None

    def __init__(self, grants: set) -> None:
        from karox.models import Origin, OriginKind
        from karox.policy import CapabilityPolicy

        self.hosted_origin = Origin(
            OriginKind.HOSTED_CLIENT, "core-bridge-hosted-session-1"
        )
        self.policy = CapabilityPolicy(AccessProfile.BROWSER_CONTROL)
        self.policy.set_grants(self.hosted_origin, grants)


def test_workspace_worker_browser_command_fails_closed_and_attaches_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hosted browser.command entry point runs the real browser engine
    behind the runtime's capability policy, attaching lazily and failing
    closed when the runtime has no policy."""

    from karox import workspace_worker
    from karox.browser import BrowserToolRuntime

    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))

    class Bare:
        session_id = "bare"

    with pytest.raises(workspace_worker.InvalidCommand):
        workspace_worker.execute_browser_command(
            Bare(), {"action": "tabs", "payload": {}}, 1.0
        )

    denied = _HostedRuntime(set())
    refused = workspace_worker.execute_browser_command(
        denied, {"action": "tabs", "payload": {}}, 1.0
    )
    # The guard refusal is a structured typed envelope, and the browser was
    # never launched.
    assert refused["ok"] is False
    assert refused["error_kind"] == "permission_denied"
    # The lazy runtime still attached itself for this hosted session.
    assert isinstance(denied._browser, BrowserToolRuntime)
    assert denied._browser_session_id == "hosted-session-1"
    denied._browser.close_all()


def test_workspace_worker_browser_command_opens_a_real_page(
    fixture_site: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A granted hosted runtime drives a real Chromium through the worker."""

    from karox import workspace_worker
    from karox.browser_bootstrap import playwright_chromium_status
    from karox.models import Capability

    if not playwright_chromium_status().ready:
        pytest.skip("managed chromium is not provisioned")

    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    allowed = _HostedRuntime(
        {Capability.BROWSER_READ, Capability.BROWSER_INPUT, Capability.REPO_READ}
    )
    try:
        opened = workspace_worker.execute_browser_command(
            allowed, {"action": "open", "payload": {"url": fixture_site}}, 60.0
        )
        assert opened["ok"] is True
        text = workspace_worker.execute_browser_command(
            allowed, {"action": "get_text", "payload": {"selector": "#intro"}}, 30.0
        )
        assert "Welcome to the local fixture site." in text["text"]
    finally:
        allowed._browser.close_all()
