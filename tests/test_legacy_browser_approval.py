"""Legacy MCP browser approvals use synthetic runtimes, never a real git push."""

from __future__ import annotations

import base64
import json
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from _support import SRC  # noqa: F401
from karox import browser_approval
from karox.hosted_bridge import HostedApprovalRequired
from karox.proxy_server import build_proxy_asgi_app
from test_hosted_bridge import _asgi_request, _tools_call
from test_modern_mcp_2026 import (
    _ApprovalRuntime,
    _WorkspaceApprovalRuntime,
    _run_with_lifespan,
)

# Test fixtures only; none of these values authenticate to an external service.
_TOKEN = "synthetic-legacy-bridge-token"
_PASSWORD = "synthetic-legacy-approval-password"
_BASE_URL = "https://approval.example.test"
_ARGUMENTS = {"remote": "origin", "branch": "main"}
_ACTION_DIGEST = "a" * 64


class _MutableDigestApprovalRuntime(_ApprovalRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.action_digest = _ACTION_DIGEST
        self.approval_attempts: list[str] = []

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        del idempotency_key, deadline_seconds
        raise HostedApprovalRequired(
            tool_name=tool_name,
            action_digest=self.action_digest,
            action_kind="git.push",
            risk="high",
            consequence="external",
            preview=dict(arguments),
            message="Allow one synthetic push?",
        )

    def execute_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        expected_action_digest: str,
        idempotency_key: str | None = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        self.approval_attempts.append(expected_action_digest)
        assert expected_action_digest == self.action_digest
        return super().execute_approved(
            tool_name,
            arguments,
            expected_action_digest=expected_action_digest,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )


def _legacy_call(arguments: dict[str, str] | None = None) -> dict[str, Any]:
    request = _tools_call(
        _TOKEN,
        "karox_git_push",
        dict(_ARGUMENTS if arguments is None else arguments),
    )
    request["headers"].append(("mcp-protocol-version", "2025-11-25"))
    # Exercise the SDK's legacy tools/call handler, not the modern wire adapter.
    assert "_meta" not in json.loads(request["body"])["params"]
    return request


def _app(runtime: _ApprovalRuntime) -> Any:
    return build_proxy_asgi_app(
        runtime,
        _TOKEN,
        human_approval_secret=_PASSWORD,
        human_approval_base_url=_BASE_URL,
    )


def _pending(response: Any) -> str:
    assert response.status == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "resultType" not in result
    payload = result["structuredContent"]
    assert payload["ok"] is False
    assert payload["error_code"] == "approval_browser_required"
    assert _PASSWORD not in response.text
    assert _ACTION_DIGEST not in response.text
    url = payload["approval_url"]
    assert url.startswith(_BASE_URL + "/mcp/approval?")
    return url


def _state(url: str) -> str:
    query = parse_qs(urlsplit(url).query)
    assert set(query) == {"state"}
    assert len(query["state"]) == 1
    return query["state"][0]


def _browser_request(url: str, *, password: str | None = None) -> dict[str, Any]:
    parsed = urlsplit(url)
    request: dict[str, Any] = {
        "method": "GET" if password is None else "POST",
        "path": parsed.path,
        "query_string": parsed.query.encode("ascii"),
        "headers": [("host", "127.0.0.1:8765")],
    }
    if password is not None:
        request["headers"].append(
            ("content-type", "application/x-www-form-urlencoded")
        )
        request["body"] = urlencode(
            {"state": _state(url), "password": password}
        ).encode("utf-8")
    return request


def _executed(response: Any, arguments: dict[str, str]) -> None:
    assert response.status == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "ok": True,
        "tool": "karox.git.push",
        "arguments": arguments,
        "approved": True,
    }


def test_legacy_browser_approval_requires_password_and_exact_one_shot_retry() -> None:
    runtime = _ApprovalRuntime()
    app = _app(runtime)

    async def scenario() -> None:
        request = _legacy_call()
        url = _pending(await _asgi_request(app, request))
        assert runtime.approved_digests == []

        page = await _asgi_request(app, _browser_request(url))
        assert page.status == 200
        assert "Approve one KaroX action" in page.text
        assert 'name="password"' in page.text
        assert _PASSWORD not in page.text
        assert _ACTION_DIGEST not in page.text
        assert runtime.approved_digests == []

        # Merely seeing the URL or retrying cannot authorize execution.
        assert _pending(await _asgi_request(app, request)) == url
        wrong = await _asgi_request(
            app, _browser_request(url, password="synthetic-wrong-password")
        )
        assert wrong.status == 403
        assert _pending(await _asgi_request(app, request)) == url
        assert runtime.approved_digests == []

        approved_page = await _asgi_request(
            app, _browser_request(url, password=_PASSWORD)
        )
        assert approved_page.status == 200
        assert "Action approved" in approved_page.text
        # Browser POST authorizes; only the subsequent MCP retry executes.
        assert runtime.approved_digests == []
        _executed(await _asgi_request(app, request), _ARGUMENTS)
        assert runtime.approved_digests == [_ACTION_DIGEST]

        replay_url = _pending(await _asgi_request(app, request))
        assert _state(replay_url) != _state(url)
        stale_post = await _asgi_request(
            app, _browser_request(url, password=_PASSWORD)
        )
        assert stale_post.status == 400
        _pending(await _asgi_request(app, request))
        assert runtime.approved_digests == [_ACTION_DIGEST]

    _run_with_lifespan(app, scenario)


@pytest.mark.parametrize("changed", [{"branch": "release"}, {"remote": "upstream"}])
def test_legacy_changed_arguments_cannot_consume_original_approval(
    changed: dict[str, str],
) -> None:
    runtime = _ApprovalRuntime()
    app = _app(runtime)

    async def scenario() -> None:
        original = _legacy_call()
        url = _pending(await _asgi_request(app, original))
        approved = await _asgi_request(app, _browser_request(url, password=_PASSWORD))
        assert approved.status == 200
        assert runtime.approved_digests == []

        # The synthetic runtime intentionally returns the SAME action digest for
        # every argument set: the broker must additionally bind exact arguments.
        changed_request = _legacy_call({**_ARGUMENTS, **changed})
        changed_url = _pending(await _asgi_request(app, changed_request))
        assert _state(changed_url) != _state(url)
        assert runtime.approved_digests == []
        _executed(await _asgi_request(app, original), _ARGUMENTS)
        assert runtime.approved_digests == [_ACTION_DIGEST]
        _pending(await _asgi_request(app, changed_request))
        assert runtime.approved_digests == [_ACTION_DIGEST]

    _run_with_lifespan(app, scenario)


def test_legacy_changed_action_digest_requires_fresh_browser_approval() -> None:
    runtime = _MutableDigestApprovalRuntime()
    app = _app(runtime)

    async def scenario() -> None:
        request = _legacy_call()
        original_url = _pending(await _asgi_request(app, request))
        approved = await _asgi_request(
            app, _browser_request(original_url, password=_PASSWORD)
        )
        assert approved.status == 200
        assert "Action approved" in approved.text
        assert runtime.approved_digests == []

        # Arguments remain identical, but repository state changes the action.
        # An approval for the previous digest must never reach execute_approved.
        new_digest = "b" * 64
        runtime.action_digest = new_digest
        response = await _asgi_request(app, request)
        fresh_url = _pending(response)
        assert new_digest not in response.text
        assert _state(fresh_url) != _state(original_url)
        assert runtime.approval_attempts == []
        assert runtime.approved_digests == []
        assert _pending(await _asgi_request(app, request)) == fresh_url
        assert runtime.approval_attempts == []
        assert runtime.approved_digests == []

        fresh_approval = await _asgi_request(
            app, _browser_request(fresh_url, password=_PASSWORD)
        )
        assert fresh_approval.status == 200
        assert "Action approved" in fresh_approval.text
        assert runtime.approval_attempts == []
        assert runtime.approved_digests == []
        _executed(await _asgi_request(app, request), _ARGUMENTS)
        assert runtime.approval_attempts == [new_digest]
        assert runtime.approved_digests == [new_digest]

        replay_url = _pending(await _asgi_request(app, request))
        assert _state(replay_url) != _state(fresh_url)
        assert runtime.approval_attempts == [new_digest]
        assert runtime.approved_digests == [new_digest]

    _run_with_lifespan(app, scenario)


@pytest.mark.parametrize("approve_before_expiry", [False, True])
def test_legacy_expired_pending_or_approved_state_never_executes(
    monkeypatch: pytest.MonkeyPatch, approve_before_expiry: bool,
) -> None:
    clock = SimpleNamespace(now=2_000_000_000.0)
    # Replace only the broker's clock, not Python's shared time module or AnyIO.
    monkeypatch.setattr(
        browser_approval, "time", SimpleNamespace(time=lambda: clock.now)
    )
    runtime = _ApprovalRuntime()
    app = _app(runtime)

    async def scenario() -> None:
        request = _legacy_call()
        url = _pending(await _asgi_request(app, request))
        if approve_before_expiry:
            approved = await _asgi_request(
                app, _browser_request(url, password=_PASSWORD)
            )
            assert approved.status == 200
        assert runtime.approved_digests == []
        # Beyond even the broker's maximum allowed TTL; no real sleeping.
        clock.now += 601.0
        expired_page = await _asgi_request(app, _browser_request(url))
        assert expired_page.status == 400
        expired_post = await _asgi_request(
            app, _browser_request(url, password=_PASSWORD)
        )
        assert expired_post.status == 400
        fresh_url = _pending(await _asgi_request(app, request))
        assert _state(fresh_url) != _state(url)
        assert runtime.approved_digests == []

    _run_with_lifespan(app, scenario)


@pytest.mark.parametrize(
    ("secret", "base_url"),
    [
        pytest.param(None, None, id="no-config"),
        pytest.param(None, _BASE_URL, id="absent-secret"),
        pytest.param(_PASSWORD, None, id="absent-base"),
        pytest.param(_PASSWORD, "", id="empty-base"),
        pytest.param(_PASSWORD, "http://approval.example.test", id="http-base"),
        pytest.param(
            _PASSWORD, "https://user@approval.example.test", id="https-userinfo"
        ),
        pytest.param(
            _PASSWORD, "https://user:pass@approval.example.test", id="https-user-password"
        ),
        pytest.param(
            _PASSWORD, _BASE_URL + "?redirect=elsewhere", id="https-query"
        ),
        pytest.param(_PASSWORD, _BASE_URL + "#approval", id="https-fragment"),
        pytest.param(_PASSWORD, "https:///mcp/approval", id="https-empty-hostname"),
    ],
)
def test_legacy_missing_secure_browser_configuration_fails_closed(
    secret: str | None, base_url: str | None,
) -> None:
    runtime = _ApprovalRuntime()
    app = build_proxy_asgi_app(
        runtime,
        _TOKEN,
        human_approval_secret=secret,
        human_approval_base_url=base_url,
    )

    async def scenario() -> None:
        request = _legacy_call()
        # Neither a TLS request nor forwarded host headers replace explicit config.
        request["scheme"] = "https"
        request["headers"].extend(
            [("x-forwarded-proto", "https"), ("x-forwarded-host", "untrusted.example.test")]
        )
        for _ in range(2):
            response = await _asgi_request(app, request)
            assert response.status == 200
            result = response.json()["result"]
            assert result["isError"] is True
            assert result["structuredContent"]["error_code"] == "denied"
            assert "approval_url" not in response.text
            assert _PASSWORD not in response.text
            assert runtime.approved_digests == []

    _run_with_lifespan(app, scenario)


def test_legacy_non_external_action_stays_denied_even_with_browser_config() -> None:
    runtime = _WorkspaceApprovalRuntime()
    app = _app(runtime)

    async def scenario() -> None:
        response = await _asgi_request(app, _legacy_call())
        assert response.status == 200
        result = response.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["error_code"] == "denied"
        assert "approval_url" not in response.text
        assert runtime.approved_digests == []

    _run_with_lifespan(app, scenario)


def test_legacy_browser_state_is_random_opaque_and_contains_no_action_digest() -> None:
    states: list[str] = []
    # Identical requests and credentials on independent brokers must not derive
    # their browser state deterministically from the action or arguments.
    for _ in range(2):
        runtime = _ApprovalRuntime()
        app = _app(runtime)

        async def scenario() -> None:
            response = await _asgi_request(app, _legacy_call())
            state = _state(_pending(response))
            assert re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
            decoded = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))
            assert len(decoded) == 32
            assert base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") == state
            assert _PASSWORD not in state
            assert runtime.approved_digests == []
            states.append(state)

        _run_with_lifespan(app, scenario)
    assert len(set(states)) == 2
