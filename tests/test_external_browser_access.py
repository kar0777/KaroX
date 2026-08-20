"""Security and session-isolation tests for external browser access."""

from __future__ import annotations

import concurrent.futures
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from _support import initialize_git_repository

from karox.artifacts import ArtifactStore
from karox.browser_access import (
    BrowserAccessPolicy,
    BrowserSecurityError,
    SecureBrowserSessionManager,
    SessionPinnedProxy,
    _BrowserHandle,
    _allowed_json_values,
    _connect_to_pinned_address,
    _safe_console_text,
    _safe_field_names,
    _safe_network_url,
    validate_browser_url,
)
from karox.browser_session import _validate_local_url
from karox.hosted_tools_runtime import (
    BROWSER_CLOSE,
    BROWSER_NETWORK_REQUESTS,
    BROWSER_REQUEST_TAKEOVER,
    BROWSER_RESUME_TAKEOVER,
    BROWSER_TABS,
    HostedToolsRuntime,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore


PUBLIC_DNS = [(2, 1, 6, "", ("93.184.216.34", 443))]
DUAL_STACK_DNS = [
    (10, 1, 6, "", ("2001:4860:4860::8888", 443, 0, 0)),
    (2, 1, 6, "", ("93.184.216.34", 443)),
]
PRIVATE_DNS = [(2, 1, 6, "", ("10.0.0.5", 443))]
LOOPBACK_DNS = [(2, 1, 6, "", ("127.0.0.1", 8080))]


class _FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        frame_url: str = "about:blank",
        redirected_from=None,  # noqa: ANN001
    ) -> None:
        self.url = url
        self.frame = _FakeFrame(frame_url)
        self.redirected_from = redirected_from


class _FakeRoute:
    def __init__(self, request: _FakeRequest) -> None:
        self.request = request
        self.aborted = False
        self.continued = False

    def abort(self) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class _FakePage:
    def __init__(self, url: str = "https://example.com/") -> None:
        self.url = url
        self.closed = False
        self.front = False
        self.title_thread_ids: list[int] = []
        self.goto_calls: list[str] = []
        self.viewport_size = {"width": 1440, "height": 900}
        self.locator_object = _FakeLocator()

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def bring_to_front(self) -> None:
        self.front = True

    def title(self) -> str:
        self.title_thread_ids.append(threading.get_ident())
        return "Example"

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append(url)
        self.url = url

    def evaluate(self, script: str):  # noqa: ANN001
        if "document.activeElement" in script:
            return "body"
        return {
            "headings": [{"level": "h1", "text": "Example"}],
            "buttons": [],
            "inputs": [],
            "links": [],
            "dialogs": [],
            "tabs": [],
            "text": "Example",
            "scroll": {
                "width": 1440,
                "height": 900,
                "viewport_width": 1440,
                "viewport_height": 900,
                "horizontal_overflow": False,
            },
        }

    def locator(self, selector: str):  # noqa: ANN001
        self.locator_object.selector = selector
        return self.locator_object


class _BlockingTitlePage(_FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.title_started = threading.Event()
        self.title_release = threading.Event()

    def title(self) -> str:
        self.title_thread_ids.append(threading.get_ident())
        self.title_started.set()
        if not self.title_release.wait(timeout=5.0):
            raise RuntimeError("title test was not released")
        return "Example"


class _FakeLocator:
    def __init__(self, metadata=None, body_text: str = "") -> None:  # noqa: ANN001
        self.first = self
        self.metadata = metadata or {
            "type": "button",
            "name": "",
            "id": "",
            "aria": "",
            "text": "Continue",
        }
        self.body_text = body_text
        self.clicked = False
        self.selector = ""

    def evaluate(self, script: str):  # noqa: ANN001
        return dict(self.metadata)

    def click(self, timeout: int) -> None:
        self.clicked = True

    def inner_text(self, timeout: int = 0) -> str:
        return self.body_text


class _FakeCloseable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _handle(session_page: _FakePage, tab_id: str = "tab-own") -> _BrowserHandle:
    return _BrowserHandle(
        playwright_ctx=_FakePlaywright(),
        browser=_FakeCloseable(),
        context=_FakeCloseable(),
        tabs={tab_id: session_page},
        active_tab_id=tab_id,
    )


class UrlPolicyTests(unittest.TestCase):
    def test_external_https_is_allowed_only_with_explicit_permission(self) -> None:
        policy = BrowserAccessPolicy(
            session_id="external",
            external_https=True,
            allowed_domains=("example.com",),
        )
        with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
            self.assertEqual(
                validate_browser_url("https://example.com/", policy),
                "https://example.com/",
            )

    def test_old_localhost_only_policy_blocks_same_external_url(self) -> None:
        policy = BrowserAccessPolicy(session_id="local")
        with self.assertRaisesRegex(BrowserSecurityError, "limited to localhost"):
            validate_browser_url("https://example.com/", policy)

    def test_file_scheme_is_blocked(self) -> None:
        policy = BrowserAccessPolicy(session_id="s", external_https=True)
        with self.assertRaisesRegex(BrowserSecurityError, "scheme"):
            validate_browser_url("file:///etc/passwd", policy)

    def test_javascript_scheme_is_blocked(self) -> None:
        policy = BrowserAccessPolicy(session_id="s", external_https=True)
        with self.assertRaisesRegex(BrowserSecurityError, "scheme"):
            validate_browser_url("javascript:alert(1)", policy)

    def test_private_ip_is_blocked(self) -> None:
        policy = BrowserAccessPolicy(session_id="s", external_https=True)
        with self.assertRaisesRegex(BrowserSecurityError, "private or reserved"):
            validate_browser_url("https://10.0.0.5/", policy)

    def test_public_hostname_resolving_private_is_blocked(self) -> None:
        policy = BrowserAccessPolicy(session_id="s", external_https=True)
        with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PRIVATE_DNS):
            with self.assertRaisesRegex(BrowserSecurityError, "private or reserved"):
                validate_browser_url("https://public.example/redirected", policy)

    def test_redirect_target_to_private_is_revalidated_and_blocked(self) -> None:
        policy = BrowserAccessPolicy(session_id="s", external_https=True)
        with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
            validate_browser_url("https://example.com/start", policy)
        with self.assertRaises(BrowserSecurityError):
            validate_browser_url("https://169.254.169.254/latest/meta-data", policy)

    def test_external_frame_cannot_redirect_or_request_localhost(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            manager = SecureBrowserSessionManager(
                ArtifactStore("route-policy"),
                BrowserAccessPolicy(
                    session_id="route-policy",
                    localhost=True,
                    external_https=True,
                ),
            )
            redirected = _FakeRequest("https://example.com/start")
            route = _FakeRoute(
                _FakeRequest(
                    "http://127.0.0.1:8080/admin",
                    frame_url="https://example.com/start",
                    redirected_from=redirected,
                )
            )
            manager._route(route)
            self.assertTrue(route.aborted)
            self.assertFalse(route.continued)

            # A deliberate agent navigation is different from an external page
            # attempting local-network access and remains available.
            manager._explicit_navigation_target = "http://127.0.0.1:8080/admin"
            direct = _FakeRoute(
                _FakeRequest(
                    "http://127.0.0.1:8080/admin",
                    frame_url="https://example.com/start",
                )
            )
            manager._route(direct)
            self.assertFalse(direct.aborted)
            self.assertTrue(direct.continued)

    def test_legacy_localhost_validator_remains_localhost_only(self) -> None:
        self.assertEqual(
            _validate_local_url("http://127.0.0.1:8080/"),
            "http://127.0.0.1:8080/",
        )
        with self.assertRaises(BrowserSecurityError):
            _validate_local_url("https://example.com/")


class ProxyPinningTests(unittest.TestCase):
    def test_pinned_connect_uses_validated_ip_not_hostname(self) -> None:
        policy = BrowserAccessPolicy(session_id="pin", external_https=True)
        upstream = mock.Mock()
        with mock.patch(
            "karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS
        ), mock.patch(
            "karox.browser_access.socket.create_connection", return_value=upstream
        ) as connect:
            result = _connect_to_pinned_address(
                "https", "example.com", 443, policy
            )
        self.assertIs(result, upstream)
        connect.assert_called_once_with(("93.184.216.34", 443), timeout=3.0)

    def test_pinned_connect_prefers_ipv4_when_unreachable_ipv6_is_listed_first(self) -> None:
        policy = BrowserAccessPolicy(session_id="pin-dual", external_https=True)
        upstream = mock.Mock()
        with mock.patch(
            "karox.browser_access.socket.getaddrinfo", return_value=DUAL_STACK_DNS
        ), mock.patch(
            "karox.browser_access.socket.create_connection", return_value=upstream
        ) as connect:
            result = _connect_to_pinned_address(
                "https", "example.com", 443, policy
            )
        self.assertIs(result, upstream)
        connect.assert_called_once_with(("93.184.216.34", 443), timeout=3.0)

    def test_pinned_connect_rejects_private_dns_before_socket_connect(self) -> None:
        policy = BrowserAccessPolicy(session_id="pin", external_https=True)
        with mock.patch(
            "karox.browser_access.socket.getaddrinfo", return_value=PRIVATE_DNS
        ), mock.patch("karox.browser_access.socket.create_connection") as connect:
            with self.assertRaises(BrowserSecurityError):
                _connect_to_pinned_address("https", "public.example", 443, policy)
        connect.assert_not_called()

    def test_session_proxy_requires_its_random_credentials(self) -> None:
        proxy = SessionPinnedProxy(
            BrowserAccessPolicy(session_id="proxy-auth", external_https=True)
        )
        proxy.start()
        try:
            host_port = proxy.server_url.removeprefix("http://")
            host, raw_port = host_port.rsplit(":", 1)
            with socket.create_connection((host, int(raw_port)), timeout=3.0) as client:
                client.sendall(
                    b"CONNECT example.com:443 HTTP/1.1\r\n"
                    b"Host: example.com:443\r\n\r\n"
                )
                response = client.recv(4096)
            self.assertIn(b"407 Proxy Authentication Required", response)
            self.assertIn(b"Proxy-Authenticate: Basic", response)
        finally:
            proxy.stop()


class IsolationAndTakeoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_runtime = os.environ.get("KAROX_VNEXT_RUNTIME_DIR")
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(Path(self.temp.name) / "runtime")

    def tearDown(self) -> None:
        if self.old_runtime is None:
            os.environ.pop("KAROX_VNEXT_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = self.old_runtime
        self.temp.cleanup()

    def _manager(self, session_id: str, *, takeover: bool = False) -> SecureBrowserSessionManager:
        return SecureBrowserSessionManager(
            ArtifactStore(session_id),
            BrowserAccessPolicy(
                session_id=session_id,
                external_https=True,
                headed=takeover,
                user_takeover=takeover,
            ),
        )

    def test_two_sessions_have_distinct_managers_contexts_and_artifact_scopes(self) -> None:
        a = self._manager("session-a")
        b = self._manager("session-b")
        a._handle = _handle(_FakePage(), "tab-a")
        b._handle = _handle(_FakePage(), "tab-b")
        self.assertIsNot(a._handle.context, b._handle.context)
        self.assertNotEqual(a.policy.session_id, b.policy.session_id)
        self.assertNotEqual(a._artifacts.root, b._artifacts.root)

    def test_session_cannot_switch_or_close_foreign_tab(self) -> None:
        b = self._manager("session-b")
        b._handle = _handle(_FakePage(), "tab-b")
        with self.assertRaisesRegex(BrowserSecurityError, "does not belong"):
            b.switch_tab({"tab_id": "tab-a"}, 5)
        with self.assertRaisesRegex(BrowserSecurityError, "does not belong"):
            b.close_tab({"tab_id": "tab-a"}, 5)

    def test_close_releases_only_own_context(self) -> None:
        a = self._manager("session-a")
        b = self._manager("session-b")
        a._handle = _handle(_FakePage(), "tab-a")
        b._handle = _handle(_FakePage(), "tab-b")
        b_context = b._handle.context
        a_context = a._handle.context
        a.close()
        self.assertTrue(a_context.closed)
        self.assertFalse(b_context.closed)
        self.assertTrue(b.is_open)

    def test_takeover_blocks_parallel_agent_input(self) -> None:
        manager = self._manager("session-a", takeover=True)
        manager._handle = _handle(_FakePage(), "tab-a")
        result = manager.request_user_takeover({"reason": "Google login"}, 5)
        self.assertTrue(result["agent_input_paused"])
        with self.assertRaisesRegex(BrowserSecurityError, "user has control"):
            manager.click({"selector": "button"}, 5)

    def test_takeover_brings_owned_system_chrome_to_front(self) -> None:
        manager = self._manager("session-focus", takeover=True)
        page = _FakePage()
        manager._handle = _handle(page, "tab-focus")
        process = mock.Mock()
        manager._handle.system_chrome = mock.Mock(process=process)
        with mock.patch(
            "karox.browser_access.activate_chrome_window", return_value=True
        ) as activate:
            result = manager.request_user_takeover({"reason": "login"}, 5)
        self.assertTrue(page.front)
        self.assertTrue(result["window_activated"])
        activate.assert_called_once_with(process)

    def test_resume_keeps_same_tab_context_and_cookie_state(self) -> None:
        manager = self._manager("session-a", takeover=True)
        page = _FakePage()
        manager._handle = _handle(page, "tab-stable")
        context = manager._handle.context
        context.cookies_state = {"signed_in": "yes"}
        context_id = manager.context_id
        takeover = manager.request_user_takeover({}, 5)
        self.assertEqual(takeover["context_id"], context_id)
        with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
            result = manager.resume_after_user_takeover({}, 5)
        self.assertEqual(result["tab_id"], "tab-stable")
        self.assertEqual(result["context_id"], context_id)
        self.assertIs(manager._handle.context, context)
        self.assertEqual(context.cookies_state, {"signed_in": "yes"})
        self.assertFalse(manager.takeover_active)

    def test_about_blank_after_reconnect_recovers_same_context_and_cookies(self) -> None:
        manager = self._manager("reconnect-blank", takeover=True)
        page = _FakePage("https://example.com/account")
        manager._handle = _handle(page, "tab-stable")
        handle = manager._handle
        handle.last_safe_urls["tab-stable"] = "https://example.com/account"
        context = handle.context
        context.cookies_state = {"signed_in": "yes"}
        context_id = handle.context_id

        manager.request_user_takeover({"reason": "login"}, 5)
        page.url = "about:blank"
        with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
            resumed = manager.resume_after_user_takeover({}, 5)

        self.assertTrue(resumed["recovered_blank"])
        self.assertEqual(resumed["context_id"], context_id)
        self.assertEqual(resumed["tab_id"], "tab-stable")
        self.assertEqual(resumed["url"], "https://example.com/account")
        self.assertEqual(page.goto_calls, ["https://example.com/account"])
        self.assertIs(manager._handle.context, context)
        self.assertEqual(context.cookies_state, {"signed_in": "yes"})
        manager.close(force=True)

    def test_snapshot_recovers_about_blank_that_appears_after_resume(self) -> None:
        manager = self._manager("late-blank", takeover=True)
        page = _FakePage("https://example.com/account")
        manager._handle = _handle(page, "tab-stable")
        handle = manager._handle
        handle.last_safe_urls["tab-stable"] = "https://example.com/account"
        handle.context.cookies_state = {"signed_in": "yes"}
        context = handle.context
        context_id = handle.context_id

        manager.request_user_takeover({"reason": "login"}, 5)
        with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
            resumed = manager.resume_after_user_takeover({}, 5)
        self.assertFalse(resumed["recovered_blank"])

        # Reproduce the real race: Chromium changes to about:blank only after
        # resume returned. The next page-dependent command must heal it first.
        page.url = "about:blank"
        with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
            result = manager.snapshot({}, 5)

        self.assertEqual(result["context_id"], context_id)
        self.assertEqual(result["tab_id"], "tab-stable")
        self.assertEqual(result["url"], "https://example.com/account")
        self.assertEqual(page.goto_calls, ["https://example.com/account"])
        self.assertIs(manager._handle.context, context)
        self.assertEqual(context.cookies_state, {"signed_in": "yes"})
        manager.close(force=True)

    def test_close_is_refused_while_user_takeover_is_active(self) -> None:
        manager = self._manager("takeover-close-guard", takeover=True)
        manager._handle = _handle(_FakePage(), "tab-stable")
        context = manager._handle.context
        manager.request_user_takeover({}, 5)
        with self.assertRaisesRegex(BrowserSecurityError, "takeover is active"):
            manager.close()
        self.assertTrue(manager.is_open)
        self.assertFalse(context.closed)
        manager.close(force=True)

    def test_parallel_mcp_callers_share_one_playwright_owner_thread(self) -> None:
        manager = self._manager("thread-owned")
        page = _FakePage()
        manager._handle = _handle(page, "tab-owned")
        main_thread_id = threading.get_ident()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(lambda _index: manager.tabs({}, 5), range(12))
            )
        self.assertTrue(all(result["count"] == 1 for result in results))
        self.assertEqual(len(set(page.title_thread_ids)), 1)
        self.assertNotEqual(page.title_thread_ids[0], main_thread_id)
        manager.close()

    def test_close_waits_for_an_in_flight_browser_operation(self) -> None:
        manager = self._manager("ordered-close")
        page = _BlockingTitlePage()
        manager._handle = _handle(page, "tab-owned")
        context = manager._handle.context
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            tabs_future = executor.submit(manager.tabs, {}, 5)
            self.assertTrue(page.title_started.wait(timeout=2.0))
            close_future = executor.submit(manager.close)
            self.assertFalse(close_future.done())
            self.assertFalse(context.closed)
            page.title_release.set()
            self.assertEqual(tabs_future.result(timeout=5.0)["count"], 1)
            close_result = close_future.result(timeout=5.0)
        self.assertTrue(close_result["context_closed"])
        self.assertTrue(context.closed)

    def test_payment_action_is_blocked_without_capability(self) -> None:
        manager = self._manager("session-a")
        page = _FakePage()
        page.locator_object = _FakeLocator(
            metadata={
                "type": "button",
                "name": "checkout",
                "id": "buy-now",
                "aria": "Buy now",
                "text": "Buy now",
            }
        )
        manager._handle = _handle(page, "tab-a")
        with self.assertRaisesRegex(BrowserSecurityError, "payment or subscription"):
            manager.click({"selector": "#buy-now"}, 5)
        self.assertFalse(page.locator_object.clicked)


class NetworkRedactionTests(unittest.TestCase):
    def test_console_collapses_embedded_data_and_base64_blobs(self) -> None:
        message = "CSP blocked data:font/woff;base64," + ("A" * 5000)
        safe = _safe_console_text(message)
        self.assertIn("data:[REDACTED]", safe)
        self.assertLessEqual(len(safe), 1200)
        self.assertNotIn("A" * 200, safe)

    def test_sensitive_network_values_are_never_returned(self) -> None:
        payload = {
            "Authorization": "Bearer top-secret-token",
            "cookie": "session=secret",
            "access_token": "abc123",
            "nested": {"csrf_token": "secret", "normal": "ok"},
            "model": "claude-opus-5",
            "credits": 300,
        }
        values = _allowed_json_values(payload)
        fields = _safe_field_names(payload)
        self.assertNotIn("Authorization", fields)
        self.assertNotIn("cookie", fields)
        self.assertNotIn("access_token", values)
        self.assertEqual(values["model"], "claude-opus-5")
        self.assertEqual(values["credits"], 300)

    def test_url_query_values_and_high_entropy_segments_are_redacted(self) -> None:
        safe = _safe_network_url(
            "https://api.example.com/abcdefghijklmnopqrstuvwxyz0123456789/chat?token=secret&model=opus#fragment"
        )
        self.assertNotIn("secret", safe)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", safe)
        self.assertNotIn("fragment", safe)
        self.assertIn("model=%5BREDACTED%5D", safe)


class HostedContractTests(unittest.TestCase):
    def test_external_browser_tools_are_exposed_by_standard_hosted_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            initialize_git_repository(repository)
            sessions = SessionStore(root / "sessions")
            sessions.create(
                repository,
                "external browser",
                AccessProfile.BROWSER_CONTROL,
                session_id="browser-session",
            )
            runtime = HostedToolsRuntime(
                repository,
                sessions,
                "browser-session",
                (
                    BROWSER_TABS,
                    BROWSER_NETWORK_REQUESTS,
                    BROWSER_REQUEST_TAKEOVER,
                    BROWSER_RESUME_TAKEOVER,
                ),
                access_profile=AccessProfile.BROWSER_CONTROL,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "browser-contract"),
                browser_policy=BrowserAccessPolicy(
                    session_id="browser-session",
                    external_https=True,
                    headed=True,
                    user_takeover=True,
                    network_inspection=True,
                ),
            )
            self.assertEqual(
                [item.name for item in runtime.descriptors()],
                [
                    BROWSER_TABS,
                    BROWSER_NETWORK_REQUESTS,
                    BROWSER_REQUEST_TAKEOVER,
                    BROWSER_RESUME_TAKEOVER,
                ],
            )
            info = runtime.session_info()
            self.assertTrue(info["browser_permission"]["external_https"])
            self.assertTrue(info["browser_isolation"]["context_per_session"])
            self.assertFalse(info["browser_isolation"]["cross_session_control"])
            self.assertTrue(info["browser_persistent_across_calls"])
            runtime.cleanup_session()

    def test_first_command_after_runtime_rebuild_recovers_about_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            initialize_git_repository(repository)
            sessions = SessionStore(root / "sessions")
            session_id = "runtime-reconnect-blank"
            sessions.create(
                repository,
                "runtime reconnect blank",
                AccessProfile.BROWSER_CONTROL,
                session_id=session_id,
            )
            policy = BrowserAccessPolicy(
                session_id=session_id,
                external_https=True,
                headed=True,
                user_takeover=True,
            )
            runtime_a = HostedToolsRuntime(
                repository,
                sessions,
                session_id,
                (BROWSER_TABS,),
                access_profile=AccessProfile.BROWSER_CONTROL,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "runtime-blank-a"),
                browser_policy=policy,
            )
            page = _FakePage("about:blank")
            runtime_a._browser._handle = _handle(page, "tab-reconnect")
            handle = runtime_a._browser._handle
            handle.last_safe_urls["tab-reconnect"] = "https://example.com/dashboard"
            handle.context.cookies_state = {"login": "preserved"}
            context = handle.context
            context_id = handle.context_id

            runtime_b = HostedToolsRuntime(
                repository,
                sessions,
                session_id,
                (BROWSER_TABS,),
                access_profile=AccessProfile.BROWSER_CONTROL,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "runtime-blank-b"),
                browser_policy=policy,
            )
            with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
                result = runtime_b.execute(BROWSER_TABS, {}, deadline_seconds=5)
            payload = result.structuredContent

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["context_id"], context_id)
            self.assertEqual(payload["active_tab_id"], "tab-reconnect")
            self.assertEqual(payload["tabs"][0]["url"], "https://example.com/dashboard")
            self.assertEqual(page.goto_calls, ["https://example.com/dashboard"])
            self.assertIs(runtime_b._browser._handle.context, context)
            self.assertEqual(context.cookies_state, {"login": "preserved"})
            runtime_b.cleanup_session()

    def test_browser_close_requires_confirmation_and_cannot_interrupt_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            initialize_git_repository(repository)
            sessions = SessionStore(root / "sessions")
            session_id = "confirmed-browser-close"
            sessions.create(
                repository,
                "confirmed browser close",
                AccessProfile.BROWSER_CONTROL,
                session_id=session_id,
            )
            runtime = HostedToolsRuntime(
                repository,
                sessions,
                session_id,
                (BROWSER_CLOSE,),
                access_profile=AccessProfile.BROWSER_CONTROL,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "browser-close-contract"),
                browser_policy=BrowserAccessPolicy(
                    session_id=session_id,
                    external_https=True,
                    headed=True,
                    user_takeover=True,
                ),
            )
            runtime._browser._handle = _handle(_FakePage(), "tab-close")
            missing_confirmation = runtime.execute(BROWSER_CLOSE, {}, deadline_seconds=5)
            self.assertTrue(missing_confirmation.isError)
            self.assertTrue(runtime._browser.is_open)

            runtime._browser.request_user_takeover({}, 5)
            during_takeover = runtime.execute(
                BROWSER_CLOSE,
                {"user_confirmed": True, "reason": "user asked to close"},
                deadline_seconds=5,
            )
            self.assertTrue(during_takeover.isError)
            self.assertTrue(runtime._browser.is_open)
            runtime.cleanup_session()

    def test_rebuilt_runtime_reuses_same_browser_context_takeover_and_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            initialize_git_repository(repository)
            sessions = SessionStore(root / "sessions")
            session_id = "persistent-browser-session"
            sessions.create(
                repository,
                "persistent browser",
                AccessProfile.BROWSER_CONTROL,
                session_id=session_id,
            )
            policy = BrowserAccessPolicy(
                session_id=session_id,
                external_https=True,
                headed=True,
                user_takeover=True,
            )
            tools = (
                BROWSER_TABS,
                BROWSER_REQUEST_TAKEOVER,
                BROWSER_RESUME_TAKEOVER,
            )
            runtime_a = HostedToolsRuntime(
                repository,
                sessions,
                session_id,
                tools,
                access_profile=AccessProfile.BROWSER_CONTROL,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "browser-runtime-a"),
                browser_policy=policy,
            )
            page = _FakePage()
            runtime_a._browser._handle = _handle(page, "tab-persistent")
            context = runtime_a._browser._handle.context
            context.cookies_state = {"login": "preserved"}
            takeover = runtime_a._browser.request_user_takeover({}, 5)

            runtime_b = HostedToolsRuntime(
                repository,
                sessions,
                session_id,
                tools,
                access_profile=AccessProfile.BROWSER_CONTROL,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "browser-runtime-b"),
                browser_policy=policy,
            )
            self.assertIs(runtime_b._browser, runtime_a._browser)
            self.assertTrue(runtime_b._browser.takeover_active)
            self.assertEqual(runtime_b._browser.context_id, takeover["context_id"])
            with mock.patch("karox.browser_access.socket.getaddrinfo", return_value=PUBLIC_DNS):
                resumed = runtime_b._browser.resume_after_user_takeover({}, 5)
            self.assertEqual(resumed["tab_id"], "tab-persistent")
            self.assertIs(runtime_b._browser._handle.context, context)
            self.assertEqual(context.cookies_state, {"login": "preserved"})
            runtime_b.cleanup_session()


class LauncherAndProfileTests(unittest.TestCase):
    def test_external_config_diagnostics_and_child_argv_are_explicit(self) -> None:
        from karox.web_bridge_launcher import (
            WebBridgeConnectConfig,
            _bridge_argv,
            web_bridge_diagnostics,
        )

        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            access_profile=AccessProfile.BROWSER_CONTROL,
            tunnel="custom",
            public_url="https://bridge.example",
            browser_external_https=True,
            browser_allowed_domains=("example.com",),
            browser_headed=True,
            browser_user_takeover=True,
            browser_network_inspection=True,
            browser_allowed_emails=("egor@example.com",),
        )
        diagnostics = web_bridge_diagnostics(config, session_id="browser-session")
        permission = diagnostics["browser_permission"]
        self.assertTrue(permission["external_https"])
        self.assertTrue(permission["user_takeover"])
        self.assertTrue(permission["network_inspection"])
        self.assertFalse(permission["localhost_only"])
        self.assertFalse(diagnostics["write_permission"])
        self.assertEqual(diagnostics["url_policy"]["private_network"], "blocked")
        self.assertTrue(diagnostics["browser_isolation"]["context_per_session"])
        self.assertEqual(permission["backend"], "extension")
        self.assertTrue(diagnostics["browser_isolation"]["dedicated_chrome_profile"])
        self.assertFalse(diagnostics["browser_isolation"]["main_chrome_profile_visible"])
        self.assertFalse(diagnostics["browser_isolation"]["dns_pinning_proxy"])
        self.assertEqual(
            diagnostics["browser_isolation"]["proxy_authentication"],
            "not_used",
        )
        self.assertEqual(
            diagnostics["url_policy"]["external_to_localhost"], "blocked"
        )
        self.assertEqual(
            diagnostics["url_policy"]["dns_rebinding"],
            "navigation_validation_only",
        )
        argv = _bridge_argv(
            config,
            session_id="browser-session",
            public_url="https://bridge.example",
        )
        self.assertIn("--browser-external-https", argv)
        self.assertIn("--browser-headed", argv)
        self.assertIn("--browser-user-takeover", argv)
        self.assertIn("--browser-network-inspection", argv)
        self.assertIn("example.com", argv)
        self.assertIn("egor@example.com", argv)

    def test_external_config_refuses_read_only_access(self) -> None:
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        with self.assertRaisesRegex(ValueError, "browser_control"):
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                access_profile=AccessProfile.READ_ONLY,
                browser_external_https=True,
            )

    def test_cli_parser_accepts_external_browser_policy(self) -> None:
        from karox.cli import _parser

        args = _parser().parse_args(
            [
                "bridge",
                "connect",
                "chatgpt-web",
                "--repository",
                str(Path.cwd()),
                "--browser-external-https",
                "--browser-domain",
                "example.com",
                "--browser-headed",
                "--browser-user-takeover",
                "--browser-network-inspection",
                "--browser-allowed-email",
                "egor@example.com",
                "--diagnostics-only",
            ]
        )
        self.assertTrue(args.browser_external_https)
        self.assertEqual(args.browser_domain, ["example.com"])
        self.assertTrue(args.browser_user_takeover)
        self.assertEqual(args.browser_allowed_email, ["egor@example.com"])

    def test_legacy_browser_input_does_not_auto_enable_network_inspection(self) -> None:
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            tools=("karox.browser.open", "karox.browser.click"),
        )
        self.assertIn("karox.browser.snapshot", config.tools)
        self.assertNotIn("karox.browser.network_requests", config.tools)

    def test_saved_profile_rejects_loose_boolean_and_domain_types(self) -> None:
        from karox.web_bridge_profiles import SavedWebBridgeProfile, WebBridgeProfileError

        base = {
            "name": "bad-browser",
            "target_profile": "chatgpt-web",
            "tools": ["karox.repo.read_file"],
            "access_profile": "browser_control",
        }
        with self.assertRaisesRegex(WebBridgeProfileError, "must be boolean"):
            SavedWebBridgeProfile.from_dict(
                {**base, "browser_external_https": "false"}
            )
        with self.assertRaisesRegex(WebBridgeProfileError, "string list"):
            SavedWebBridgeProfile.from_dict(
                {
                    **base,
                    "browser_external_https": True,
                    "browser_allowed_domains": "example.com",
                }
            )

    def test_saved_profile_roundtrip_preserves_external_browser_policy(self) -> None:
        from karox.web_bridge_profiles import SavedWebBridgeProfile

        profile = SavedWebBridgeProfile(
            name="chatgpt-browser",
            target_profile="chatgpt-web",
            tools=("karox.repo.read_file",),
            access_profile=AccessProfile.BROWSER_CONTROL,
            tunnel="tailscale",
            browser_external_https=True,
            browser_allowed_domains=("example.com",),
            browser_denied_domains=("blocked.example",),
            browser_headed=True,
            browser_user_takeover=True,
            browser_network_inspection=True,
            browser_allowed_emails=("egor@example.com",),
        )
        restored = SavedWebBridgeProfile.from_dict(profile.to_dict())
        self.assertEqual(restored, profile)
        self.assertEqual(restored.browser_allowed_domains, ("example.com",))
        self.assertTrue(restored.browser_user_takeover)


if __name__ == "__main__":
    unittest.main()
