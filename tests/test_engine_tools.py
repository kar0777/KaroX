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

from karox.core import InvalidCommand
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


def _attach_browser(runtime: Any) -> None:
    """The one attach step the live entry point expects from its host."""
    from karox.browser import BrowserAccessPolicy, BrowserToolRuntime
    from karox.paths import runtime_dir

    def guard(capability: Any) -> None:
        runtime.policy.require(runtime.hosted_origin, capability)

    runtime._browser = BrowserToolRuntime(
        BrowserAccessPolicy(session_id="ad-hoc"),
        browser_root=runtime_dir() / "vnext" / "browser",
        capability_guard=guard,
    )
    runtime._browser_session_id = "hosted-session-1"


def test_browser_command_entry_point_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser.command entry point refuses a runtime it cannot guard."""

    from karox.browser.tools import execute_browser_command

    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))

    class Bare:
        session_id = "bare"

    with pytest.raises(InvalidCommand):
        execute_browser_command(Bare(), {"action": "tabs", "payload": {}}, 1.0)

    denied = _HostedRuntime(set())
    _attach_browser(denied)
    refused = execute_browser_command(
        denied, {"action": "tabs", "payload": {}}, 1.0
    )
    # The guard refusal is a structured typed envelope, and the browser was
    # never launched.
    assert refused["ok"] is False
    assert refused["error_kind"] == "permission_denied"
    denied._browser.close_all()


def test_browser_command_entry_point_opens_a_real_page(
    fixture_site: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A granted runtime drives a real Chromium through the live entry point."""

    from karox.browser.tools import execute_browser_command
    from karox.browser_bootstrap import playwright_chromium_status
    from karox.models import Capability

    if not playwright_chromium_status().ready:
        pytest.skip("managed chromium is not provisioned")

    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    allowed = _HostedRuntime(
        {Capability.BROWSER_READ, Capability.BROWSER_INPUT, Capability.REPO_READ}
    )
    _attach_browser(allowed)
    try:
        opened = execute_browser_command(
            allowed, {"action": "open", "payload": {"url": fixture_site}}, 60.0
        )
        assert opened["ok"] is True
        text = execute_browser_command(
            allowed, {"action": "get_text", "payload": {"selector": "#intro"}}, 30.0
        )
        assert "Welcome to the local fixture site." in text["text"]
    finally:
        allowed._browser.close_all()

def test_engine_browser_policy_enables_downloads_for_the_session(tmp_path: Path) -> None:
    """The default agent-run browser may save a download it was asked for.

    Downloads land in KaroX's artifact store, never the repository, so the
    missing permission was the only thing standing between a browser task and
    its requested deliverable. The runtime's policy is the one every
    per-session adapter inherits.
    """
    from karox.browser import BrowserAccessPolicy
    from karox.core import CoreRuntime
    from karox.engine_tools import register_browser_tool
    from karox.models import Origin, OriginKind
    from karox.policy import CapabilityPolicy
    from karox.sessions import SessionStore

    repository = tmp_path / "repo"
    repository.mkdir()
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    core = CoreRuntime(
        repository,
        policy,
        SessionStore(tmp_path / "sessions"),
        tmp_path / "audit.jsonl",
    )
    origin = Origin(OriginKind.NATIVE_AGENT, "engine-tools-test")
    runtime = register_browser_tool(
        core, capability_policy=policy, origin=origin, session_id="s-downloads"
    )

    assert isinstance(runtime.policy, BrowserAccessPolicy)
    assert runtime.policy.downloads is True
    assert runtime.policy.session_id == "s-downloads"
    assert runtime.policy.external_https is False
    assert runtime.policy.allow_detach is False
    # Credentials are a separate, explicit per-run permission.
    assert runtime.policy.plain_credentials is False

    second = CoreRuntime(
        repository,
        policy,
        SessionStore(tmp_path / "sessions"),
        tmp_path / "audit-2.jsonl",
    )
    permitted = register_browser_tool(
        second,
        capability_policy=policy,
        origin=origin,
        session_id="s-credentials",
        plain_credentials=True,
    )
    assert permitted.policy.plain_credentials is True


def test_desktop_tool_accepts_integer_arguments_through_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race failure end to end: window_id, x and y reach a real adapter.

    The desktop tools are registered as built-in Core tools. Before the
    validator unification every integer argument was refused as an unsupported
    schema type, so the handler never ran and the computer-use benchmark could
    not click anything. A fake probe proves the whole path: policy grant,
    validation, adapter dispatch and the posted input messages.
    """
    from _computer_use_fake import FakeProbe, FakeWindow

    from karox import engine_tools
    from karox.computer_use.adapter import ComputerUseAdapter
    from karox.core import CoreCommand, CoreRuntime
    from karox.models import Capability, Origin, OriginKind
    from karox.policy import CapabilityPolicy
    from karox.sessions import SessionStore

    probe = FakeProbe()
    probe.add_window(
        FakeWindow(1001, "KaroX Test Editor", "Notepad", (100, 100, 900, 700), 4242)
    )
    adapter = ComputerUseAdapter(
        runtime_root=tmp_path / "cu", session_id="s-desktop", probe=probe
    )
    monkeypatch.setattr(
        engine_tools, "ComputerUseAdapter", lambda session_id: adapter
    )

    repository = tmp_path / "repo"
    repository.mkdir()
    origin = Origin(OriginKind.NATIVE_AGENT, "desktop-test")
    policy = CapabilityPolicy(AccessProfile.ELEVATED)
    policy.set_grants(origin, {Capability.DESKTOP_INPUT})
    store = SessionStore(tmp_path / "sessions")
    store.create(
        repository,
        "desktop integration",
        AccessProfile.ELEVATED,
        session_id="s-desktop",
    )
    core = CoreRuntime(repository, policy, store, tmp_path / "audit.jsonl")
    engine_tools.register_desktop_tools(core, session_id="s-desktop")

    attached = core.execute(
        CoreCommand(
            "desktop.attach",
            {"app_id": "editor", "window_id": 1001, "access": "control"},
            "s-desktop",
            origin,
            deadline_seconds=10.0,
        )
    )
    assert attached.ok is True, attached.data
    assert attached.data["ok"] is True
    assert attached.data["data"]["attached"] is True

    clicked = core.execute(
        CoreCommand(
            "desktop.click",
            {"app_id": "editor", "x": 10, "y": 10},
            "s-desktop",
            origin,
            deadline_seconds=10.0,
        )
    )
    assert clicked.ok is True, clicked.data
    assert clicked.data["ok"] is True
    assert probe.posted, "the click never reached the fake window"

    # The race's second failure mode: the whole call wrapped in ``payload``,
    # copying browser.command's shape. It is flattened and still runs.
    probe.posted.clear()
    wrapped = core.execute(
        CoreCommand(
            "desktop.click",
            {"payload": {"app_id": "editor", "x": 5, "y": 6}},
            "s-desktop",
            origin,
            deadline_seconds=10.0,
        )
    )
    assert wrapped.ok is True, wrapped.data
    assert wrapped.data["ok"] is True
    assert probe.posted, "the wrapped click never reached the fake window"


def test_cli_engine_run_logs_in_and_downloads_a_report(
    fixture_site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole default surface: login with test credentials, then download.

    The browser-use race failed exactly here: the password fill was refused,
    downloads were disabled, and the ``get_text`` after the redirect had no
    selector. This runs the real CLI path with a scripted model and the real
    fixture site, then checks the artifact store independently.
    """
    import contextlib
    import hashlib
    import io
    import json as json_module

    from _browser_site import DOWNLOAD_BODY, LOGIN_PASS, LOGIN_USER
    from karox import cli
    from karox.browser_bootstrap import playwright_chromium_status

    if not playwright_chromium_status().ready:
        pytest.skip("managed chromium is not provisioned")

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = _cli_repo(tmp_path)
    responses = [
        _response((_browser_call("c1", "open", {"url": f"{fixture_site}/"}),)),
        _response(
            (
                _browser_call(
                    "c2", "fill", {"selector": "#user", "value": LOGIN_USER}
                ),
                _browser_call(
                    "c3", "fill", {"selector": "#pw", "value": LOGIN_PASS}
                ),
            )
        ),
        _response(
            (_browser_call("c4", "click", {"selector": "#login-btn"}),)
        ),
        _response((_browser_call("c5", "wait_navigation", {}),)),
        _response((_browser_call("c6", "get_text", {}),)),
        _response(
            (_browser_call("c7", "open", {"url": f"{fixture_site}/"}),)
        ),
        _response(
            (_browser_call("c8", "download_start", {"selector": "#dl-link"}),)
        ),
        _response((_finish_call("c9", "downloaded report.bin after login"),)),
    ]
    provider = _scripted_cli(monkeypatch, responses)
    arguments = [
        "agent",
        "run",
        "--repository",
        str(repository),
        "--task",
        (
            f"Open {fixture_site}/, log in as {LOGIN_USER}, read the welcome "
            "page, then return to the home page and download the report."
        ),
        "--model",
        "scripted-model",
        "--base-url",
        "http://127.0.0.1:9/v1",
        "--session-id",
        "engine-1",
        "--max-steps",
        "14",
        "--max-seconds",
        "180",
        "--verification-command",
        json_module.dumps(["python", "-c", "print('ok')"]),
        "--browser-plain-credentials",
        "--stream-json",
    ]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(arguments)

    assert code == 0
    assert "browser_command" in _request_tools(provider.requests[0])
    seen = "\n".join(_request_text(request) for request in provider.requests)
    assert f"user={LOGIN_USER}" in seen
    assert "report.bin" in seen
    _, report = _parse_stream(output.getvalue())
    assert report["verified"] is True, report["reason"]
    assert report["reason"] == "answer"
    assert any(
        item.get("action") == "download_start" for item in report["answer_basis"]
    )

    # The download is a durable artifact, not an agent claim.
    expected = hashlib.sha256(DOWNLOAD_BODY).hexdigest()
    artifact_dir = tmp_path / "runtime" / "vnext" / "artifacts" / "engine-1"
    blobs = list(artifact_dir.glob("*.bin")) if artifact_dir.exists() else []
    assert any(
        hashlib.sha256(blob.read_bytes()).hexdigest() == expected for blob in blobs
    ), "the downloaded bytes are not in the artifact store"


def test_cli_engine_run_drives_the_real_desktop_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop domain end to end: attach by window id, type, capture.

    The race failed here before the handler ran: every integer argument was
    refused by the validator. This drives the real CLI with a scripted model
    against an owned background fixture window, then reads the EDIT control
    independently to prove the posted input landed.
    """
    import contextlib
    import io
    import json as json_module
    import os
    import time

    if os.name != "nt":
        pytest.skip("the desktop surface is Windows-only")

    from _desktop_test_window import TestWindow
    from karox import cli
    from karox.computer_use.win32 import default_probe
    from karox.sessions import SessionStore

    probe = default_probe()
    title = f"KaroX Desktop CLI {os.getpid()}-{int(time.time())}"
    window = TestWindow(title)
    try:
        window.start()
        # Restore the owned window without activation so thread focus can be
        # established before the scripted run starts; Windows refuses focus for
        # a minimized window.
        import ctypes as ctypes_module

        ctypes_module.windll.user32.ShowWindowAsync(window.hwnd, 4)
        time.sleep(0.2)
        window.focus_edit()
    except Exception as exc:  # noqa: BLE001 - no interactive desktop
        pytest.skip(f"cannot create the fixture window: {exc}")
    typed = "hello from the engine run"
    import threading

    keep_focus = threading.Event()

    def focus_loop() -> None:
        # The fixture refuses thread focus while minimized, and a scripted run
        # cannot pause for the test to call focus_edit at the right moment.
        # Re-assert thread focus until the run ends; this never touches the
        # global foreground.
        while not keep_focus.is_set():
            window.focus_edit(timeout=0.05)

    focus_thread = threading.Thread(target=focus_loop, daemon=True)
    try:
        focus_thread.start()
        monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
        repository = _cli_repo(tmp_path)
        task = f"Attach the window titled {title}, type into it, capture it."
        SessionStore(tmp_path / "runtime" / "vnext" / "sessions").create(
            repository,
            task,
            AccessProfile.ELEVATED,
            session_id="engine-1",
        )
        responses = [
            _response(
                (
                    _tool_call(
                        "c1", "desktop_list_windows", {"title_contains": title}
                    ),
                )
            ),
            _response(
                (
                    _tool_call(
                        "c2",
                        "desktop_attach",
                        {
                            "app_id": "fixture",
                            "window_id": window.hwnd,
                            "access": "control",
                        },
                    ),
                )
            ),
            _response(
                (
                    _tool_call(
                        "c3",
                        "desktop_show_window",
                        {"app_id": "fixture", "mode": "restore_noactivate"},
                    ),
                )
            ),
            _response(
                (
                    _tool_call(
                        "c4",
                        "desktop_inspect_ui",
                        {"app_id": "fixture", "max_nodes": 200},
                    ),
                )
            ),
            _response(
                (
                    _tool_call(
                        "c5",
                        "desktop_type_text",
                        {"app_id": "fixture", "text": typed},
                    ),
                )
            ),
            _response(
                (
                    _tool_call(
                        "c6",
                        "desktop_capture",
                        {"app_id": "fixture", "max_dim": 800},
                    ),
                )
            ),
            _response((_finish_call("c7", "attached, typed and captured"),)),
        ]
        provider = _scripted_cli(monkeypatch, responses)
        arguments = [
            "agent",
            "run",
            "--repository",
            str(repository),
            "--task",
            task,
            "--model",
            "scripted-model",
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--session-id",
            "engine-1",
            "--access-profile",
            "elevated",
            "--max-steps",
            "12",
            "--max-seconds",
            "180",
            "--verification-command",
            json_module.dumps(["python", "-c", "print('ok')"]),
            "--stream-json",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(arguments)

        assert code == 0
        assert "desktop_attach" in _request_tools(provider.requests[0])
        deadline = time.monotonic() + 10.0
        text = ""
        while time.monotonic() < deadline:
            text = probe.get_text(window.edit_hwnd, 512)
            if text == typed:
                break
            probe.sleep_ms(50)
        assert text == typed, "the typed text did not land in the EDIT control"

        _, report = _parse_stream(output.getvalue())
        assert report["verified"] is True, report["reason"]
        actions = {
            item.get("action")
            for item in report["answer_basis"]
            if item.get("tool", "").startswith("desktop.")
        }
        assert {"list_windows", "inspect_ui", "capture"} <= actions
        capture = [
            item
            for item in report["answer_basis"]
            if item.get("action") == "capture"
        ][0]
        assert capture.get("artifact_id")
        artifact_dir = (
            tmp_path / "runtime" / "vnext" / "artifacts" / "engine-1"
        )
        blobs = list(artifact_dir.glob("*.bin")) if artifact_dir.exists() else []
        assert blobs, "the capture artifact is missing"
    finally:
        keep_focus.set()
        window.stop()
