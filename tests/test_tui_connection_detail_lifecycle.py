"""The connection-detail screen must mount, and stay mounted, for every record.

Live acceptance (section 14) found a crash: opening ``ConnectionDetailScreen``
for a provider, a stopped service or a disabled service raised

    SignalError: Node must be running to subscribe to a signal

during ``mount``. The root cause is documented at the fix site in
``tui_connections.py``: the screen stored its own bridge-running flag on an
attribute named ``_running``, which is the field Textual's ``MessagePump`` owns
to mean "this node's event loop is alive". A provider sets the bridge flag
False; a stopped service sets it False; a disabled service sets it False. Each
one flipped ``screen.is_running`` to False mid-mount, and Textual refused to
let the now-"stopped" node subscribe to signals.

This is why every earlier test missed it: they intercepted ``push_screen`` and
asserted on the *constructed* screen rather than the *mounted* one, so the
crash that happens during mount never ran. The whole point of this file is to
mount the screen for real, for each record shape, and prove the lifecycle.

The crash is in mount/render, not in the data source, so the screens here are
driven through their real production read path (the same registries the hub
projects, seeded into the isolated config tree). Runtime state that needs a
live PID to report "running" is supplied by the screen's own controller, so the
mounted screen sees exactly the ``ConnectionState`` a running bridge would.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import ast
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox import tui_connections as hub
from karox.connection_controller import ConnectionState
from karox.connections import ConnectionRegistry, McpClientTarget
from karox.paths import config_dir
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry

# A real connection id, the way the registry issues them: short, stable, and
# not a preset name -- so it can never be confused with one.
CID = "c0ffee1234deadbe"

SERVICE_PRESETS = ("chatgpt-web", "claude-web", "clickup")


def _provider_record(provider_id: str = "openrouter", *, enabled: bool = True) -> ProviderRecord:
    return ProviderRecord(
        provider_id=provider_id,
        adapter_kind="openai_compatible_chat",
        base_url=f"https://{provider_id}.example/v1",
        privacy_class="public",
        enabled=enabled,
    )


def _mcp_target(
    connection_id: str = CID,
    *,
    preset_id: str = "clickup",
    name: str = "My ClickUp",
    enabled: bool = True,
) -> McpClientTarget:
    return McpClientTarget(
        connection_id=connection_id,
        name=name,
        preset_id=preset_id,
        transport="streamable_http",
        endpoint_path="/mcp",
        auth_scheme="bearer",
        tunnel="cloudflare",
        runtime_profile="generic-streamable-http",
        public_url="https://bridge.example.com",
        url_stability="temporary",
        credential_ref=f"os-keyring:connection/{connection_id}",
        credential_fingerprint="sha256:fixture",
        port=8765,
        enabled=enabled,
    )


def _runtime(state: str) -> dict[str, Any]:
    """The shape ``runtime_manager.status`` returns, for a chosen runtime state."""

    return {
        "connection_id": CID,
        "runtime_id": f"rt-{CID}",
        "state": state,
        "managed": state in {"running", "degraded"},
        "adopted": False,
        "identity_verified": False,
    }


class _DetailLifecycleBase(unittest.IsolatedAsyncioTestCase):
    """Boot the real app against an isolated config tree, then mount Detail.

    Every test ends with the screen mounted (``push_screen`` + ``pause``), so
    the mount crash -- which happens between push and the first render -- is
    the first thing this base would surface. Assertions about ``is_running``
    are the regression contract: a bridge flag of False must never read as a
    stopped screen.
    """

    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())
        enter_context(self, patch.object(tui, "_load_language", return_value="en"))
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))

    def app(self, *, language: str = "en") -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language=language)

    # -- seeding: the real production read path the screen uses -------------

    def seed_provider(
        self,
        provider_id: str = "openrouter",
        *,
        model_id: str = "claude-opus",
        enabled: bool = True,
    ) -> ProviderRegistry:
        registry = ProviderRegistry(config_dir() / "vnext" / "providers.json")
        registry.put_provider(_provider_record(provider_id, enabled=enabled))
        registry.put_model(ModelRecord(provider_id, model_id))
        return registry

    def seed_service(
        self,
        *,
        connection_id: str = CID,
        preset_id: str = "clickup",
        name: str = "My ClickUp",
        enabled: bool = True,
    ) -> ConnectionRegistry:
        # Reuse the production factory so the path stays in lockstep with the
        # one the screen's own controller reads from.
        from karox.connections import connection_registry

        registry = connection_registry()
        registry.put(
            _mcp_target(
                connection_id,
                preset_id=preset_id,
                name=name,
                enabled=enabled,
            )
        )
        return registry

    # -- mount: the real production path, not an intercepted push_screen -----

    async def mount(
        self,
        pilot,
        app,
        *,
        kind: str,
        identity: str,
        language: str = "en",
        runtime_state: str | None = None,
    ) -> Any:
        """Push the real ConnectionDetailScreen and let it fully mount.

        ``runtime_state`` is only set for a running-bridge scenario: a live
        bridge's state needs a proven PID, so the screen's own controller is
        asked to report it. Everything else reads the seeded registries as-is.
        """

        screens = app._connections_screens_cached()
        screen = screens["ConnectionDetailScreen"](
            language, kind=kind, identity=identity
        )
        if runtime_state is not None and kind == "service":
            # A frozen ConnectionState built exactly as the controller builds
            # one, so the mounted screen sees the runtime a working bridge
            # would report without us having to launch one.
            target = self.seed_service(connection_id=identity).get(identity)
            state = ConnectionState(
                target=target,
                runtime=_runtime(runtime_state),
                endpoint="https://bridge.example.com/mcp",
            )
            with patch.object(screen._controller, "get", return_value=state):
                app.push_screen(screen)
                await pilot.pause()
            return screen
        app.push_screen(screen)
        await pilot.pause()
        return screen


class DetailMountsForEveryRecordTests(_DetailLifecycleBase):
    """Seven records, one screen each -- and it stays running for all of them."""

    async def _mount_and_assert(
        self,
        *,
        kind: str,
        identity: str,
        seed=None,
        expected_status: str,
        expected_name_contains: str | None = None,
        bridge_running: bool,
        runtime_state: str | None = None,
        language: str = "en",
    ) -> Any:
        app = self.app(language=language)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            if seed is not None:
                seed()
            screen = await self.mount(
                pilot,
                app,
                kind=kind,
                identity=identity,
                language=language,
                runtime_state=runtime_state,
            )
            view = screen.view()
            # The three-line regression contract: mounted, reported as running
            # by the framework, and its bridge flag read honestly.
            self.assertIsInstance(app.screen, type(screen))
            self.assertIs(app.screen.is_running, True)
            self.assertEqual(view["status"], expected_status)
            self.assertEqual(view["running"], bridge_running)
            if expected_name_contains is not None:
                self.assertIn(expected_name_contains, view["name"])
            # The view must describe the *bridge*, never the screen lifecycle.
            self.assertEqual(view["running"], screen._bridge_running)
        return screen

    async def test_a_provider_mounts_and_stays_running(self) -> None:
        # A provider has no process, so its bridge flag is always False -- the
        # exact condition that used to flip the screen's own lifecycle off.
        await self._mount_and_assert(
            kind="provider",
            identity="openrouter",
            seed=lambda: self.seed_provider("openrouter"),
            expected_status=hub.HUB_STATUS_READY,
            expected_name_contains="OpenRouter",
            bridge_running=False,
        )

    async def test_a_provider_with_no_model_mounts(self) -> None:
        # A provider configured with no usable model falls to "needs attention"
        # rather than crashing the mount -- another False-flag record.
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            registry = ProviderRegistry(config_dir() / "vnext" / "providers.json")
            registry.put_provider(_provider_record("empty"))
            screen = await self.mount(pilot, app, kind="provider", identity="empty")
            self.assertIs(screen.is_running, True)
            self.assertEqual(screen.view()["status"], hub.HUB_STATUS_ATTENTION)
            self.assertIs(screen.view()["running"], False)

    async def test_a_stopped_chatgpt_web_mounts(self) -> None:
        await self._mount_and_assert(
            kind="service",
            identity=CID,
            seed=lambda: self.seed_service(preset_id="chatgpt-web", name="ChatGPT Web"),
            expected_status=hub.HUB_STATUS_STOPPED,
            expected_name_contains="ChatGPT",
            bridge_running=False,
        )

    async def test_a_stopped_claude_web_mounts(self) -> None:
        await self._mount_and_assert(
            kind="service",
            identity=CID,
            seed=lambda: self.seed_service(preset_id="claude-web", name="Claude Web"),
            expected_status=hub.HUB_STATUS_STOPPED,
            bridge_running=False,
        )

    async def test_a_stopped_clickup_mounts(self) -> None:
        await self._mount_and_assert(
            kind="service",
            identity=CID,
            seed=lambda: self.seed_service(preset_id="clickup", name="My ClickUp"),
            expected_status=hub.HUB_STATUS_STOPPED,
            bridge_running=False,
        )

    async def test_a_stopped_custom_mcp_mounts(self) -> None:
        await self._mount_and_assert(
            kind="service",
            identity=CID,
            seed=lambda: self.seed_service(preset_id="generic-mcp", name="Scratch API"),
            expected_status=hub.HUB_STATUS_STOPPED,
            bridge_running=False,
        )

    async def test_a_disabled_service_mounts(self) -> None:
        # Disabled drives the bridge flag False *and* the status DISABLED: two
        # reasons the old code had to flip the framework's _running.
        await self._mount_and_assert(
            kind="service",
            identity=CID,
            seed=lambda: self.seed_service(enabled=False),
            expected_status=hub.HUB_STATUS_DISABLED,
            bridge_running=False,
        )

    async def test_a_running_service_mounts_and_reports_running(self) -> None:
        # The running case is the one that *masked* the defect for months: a
        # running bridge set the flag True, which happened to agree with the
        # framework's _running, so mount succeeded. It must still succeed.
        await self._mount_and_assert(
            kind="service",
            identity=CID,
            seed=lambda: self.seed_service(),
            expected_status=hub.HUB_STATUS_WORKING,
            bridge_running=True,
            runtime_state="running",
        )


class DetailBridgeFlagIsNotScreenLifecycleTests(_DetailLifecycleBase):
    """Level-1 recurrence guard: the bridge flag and the node lifecycle are separate."""

    async def test_a_false_bridge_flag_leaves_the_screen_running(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            screen = await self.mount(
                pilot, app, kind="service", identity=CID
            )
            self.assertIs(screen.is_running, True)
            self.assertIs(screen._bridge_running, False)
            # Toggling the bridge flag during refresh must not touch lifecycle.
            screen._bridge_running = False
            screen.refresh_view()
            await pilot.pause()
            self.assertIs(screen.is_running, True)
            screen._bridge_running = True
            screen.refresh_view()
            await pilot.pause()
            self.assertIs(screen.is_running, True)
            screen._bridge_running = False
            screen.refresh_view()
            await pilot.pause()
            self.assertIs(screen.is_running, True)

    async def test_view_running_tracks_the_bridge_not_the_screen(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            screen = await self.mount(pilot, app, kind="service", identity=CID)
            for value in (False, True, False):
                screen._bridge_running = value
                self.assertEqual(screen.view()["running"], value)
                self.assertIs(screen.is_running, True)


class HubToDetailRoutingTests(_DetailLifecycleBase):
    """The real Hub -> Enter -> Detail journey, with the screen actually mounted."""

    async def _open_hub(self, pilot, app) -> Any:
        app._handle_command("/connect")
        await pilot.pause()
        screen = app.screen
        # Hub runtime aggregation is intentionally asynchronous so opening or
        # closing a modal never blocks the Textual event loop. Wait only for the
        # background snapshot to replace the temporary loading row before tests
        # address a saved connection by id.
        if type(screen).__name__ == "ConnectionHubScreen":
            options = screen.query_one("#connhub-list")
            for _ in range(20):
                ids = {
                    getattr(options.get_option_at_index(index), "id", None)
                    for index in range(len(options.options))
                }
                if "connhub-loading" not in ids:
                    break
                await pilot.pause(0.05)
        return screen

    async def test_enter_on_a_saved_service_row_mounts_the_detail_screen(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            hub_screen = await self._open_hub(pilot, app)
            self.assertEqual(type(hub_screen).__name__, "ConnectionHubScreen")
            depth = len(app.screen_stack)
            # The production routing callback, not a patched push_screen.
            app._connection_hub_done(f"{hub.HUB_FAMILY_SERVICE}:{CID}")
            await pilot.pause()
            # call_after_refresh defers the push one cycle.
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), depth + 1)
            detail = app.screen
            self.assertEqual(type(detail).__name__, "ConnectionDetailScreen")
            self.assertIs(detail.is_running, True)

    async def test_escape_from_detail_keeps_the_app_alive(self) -> None:
        """Esc on Detail dismisses it; where the app lands is the hub's contract.

        The lifecycle guarantee this suite owns is narrower: after Esc, the
        Detail screen is gone and the application is still alive with no
        crashed node left on the stack. (``_connection_detail_closed`` routes
        back through the hub rather than to the chat, which the hub tests own.)
        """

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            await self._open_hub(pilot, app)
            app._connection_hub_done(f"{hub.HUB_FAMILY_SERVICE}:{CID}")
            await pilot.pause()
            await pilot.pause()
            detail = app.screen
            self.assertEqual(type(detail).__name__, "ConnectionDetailScreen")
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            # The Detail is dismissed; nothing named ConnectionDetailScreen
            # remains on the stack, and the app never crashed.
            remaining = [
                s for s in app.screen_stack
                if type(s).__name__ == "ConnectionDetailScreen"
            ]
            self.assertEqual(remaining, [])
            for screen in app.screen_stack:
                self.assertIs(screen.is_running, True)

    async def test_three_consecutive_opens_mount_exactly_one_screen_each_time(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            for _ in range(3):
                await self._open_hub(pilot, app)
                app._connection_hub_done(f"{hub.HUB_FAMILY_SERVICE}:{CID}")
                await pilot.pause()
                await pilot.pause()
                detail = app.screen
                self.assertEqual(
                    type(detail).__name__, "ConnectionDetailScreen"
                )
                self.assertIs(detail.is_running, True)
                # Exactly one Detail on the stack each round.
                self.assertEqual(
                    sum(
                        1
                        for s in app.screen_stack
                        if type(s).__name__ == "ConnectionDetailScreen"
                    ),
                    1,
                )
                await pilot.press("escape")
                await pilot.pause()
                await pilot.pause()

    async def test_a_rapid_double_enter_mounts_only_one_detail(self) -> None:
        """Two Enters in quick succession cannot stack two detail screens.

        The routing is ``action_choose -> _open -> dismiss -> callback``, and
        ``dismiss`` pops the hub before the second keystroke can land on it, so
        the second Enter arrives at whatever is below and never re-enters the
        hub's chooser. This test pins that real-user behaviour rather than the
        router called twice by hand, because the latter never happens.
        """

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            # Use a provider row because the root Hub intentionally hides
            # stopped service history. The keyboard race is row-family agnostic;
            # what matters is that the row is genuinely visible/selectable.
            registry = self.seed_provider()
            registry.select_model("openrouter", "claude-opus")
            hub_screen = await self._open_hub(pilot, app)
            options = hub_screen.query_one("#connhub-list")
            row_id = f"{hub.HUB_FAMILY_AI}:openrouter"
            target_index = None
            for index in range(len(options.options)):
                if getattr(options.get_option_at_index(index), "id", None) == row_id:
                    target_index = index
                    break
            self.assertIsNotNone(target_index, "selected provider row must be in the hub")
            options.highlighted = target_index
            base = len(app.screen_stack)
            await pilot.press("enter")
            # Second Enter before the deferred push necessarily lands.
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            details = [
                s for s in app.screen_stack
                if type(s).__name__ == "ConnectionDetailScreen"
            ]
            self.assertLessEqual(len(details), 1, "double-Enter stacked detail screens")
            if details:
                self.assertIs(details[0].is_running, True)
            # The hub itself is gone after the first Enter dismissed it.
            self.assertLess(len(app.screen_stack), base + 2)

    async def test_a_stale_record_removed_between_render_and_enter_falls_back(self) -> None:
        """The hub is a snapshot; a vanished record must not open an empty Detail."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._open_hub(pilot, app)
            base = len(app.screen_stack)
            app._connection_hub_done(f"{hub.HUB_FAMILY_SERVICE}:never-existed")
            await pilot.pause()
            await pilot.pause()
            # Falls back to the management list, not a crashing Detail screen.
            self.assertEqual(type(app.screen).__name__, "McpClientsScreen")
            self.assertEqual(len(app.screen_stack), base + 1)


class LanguageParityOnDetailTests(_DetailLifecycleBase):
    """The screen mounts and reads honestly in both languages, and live-switches."""

    async def test_russian_detail_mounts_with_human_readable_status(self) -> None:
        app = self.app(language="ru")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            screen = await self.mount(
                pilot, app, kind="service", identity=CID, language="ru"
            )
            self.assertIs(screen.is_running, True)
            text = screen.rendered_text()
            # Russian status word for "stopped", human-readable not internal.
            self.assertIn("\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d", text)

    async def test_english_detail_mounts_with_human_readable_status(self) -> None:
        app = self.app(language="en")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            screen = await self.mount(
                pilot, app, kind="service", identity=CID, language="en"
            )
            self.assertIs(screen.is_running, True)
            self.assertIn("stopped", screen.rendered_text().casefold())

    async def test_language_can_switch_while_a_detail_is_open(self) -> None:
        app = self.app(language="en")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            screen = await self.mount(pilot, app, kind="service", identity=CID)
            self.assertIn("stopped", screen.rendered_text().casefold())
            screen.language = "ru"
            screen.refresh_view()
            await pilot.pause()
            self.assertIs(screen.is_running, True)
            self.assertIn(
                "\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d",
                screen.rendered_text(),
            )


class DetailAddressRenderingTests(_DetailLifecycleBase):
    """A provider with no address renders; a running service shows its endpoint."""

    async def test_a_provider_with_no_endpoint_mounts_without_an_address(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_provider("openrouter")
            screen = await self.mount(
                pilot, app, kind="provider", identity="openrouter"
            )
            self.assertIs(screen.is_running, True)
            self.assertEqual(screen.view()["endpoint"], "")

    async def test_a_running_service_shows_its_endpoint(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.seed_service()
            screen = await self.mount(
                pilot,
                app,
                kind="service",
                identity=CID,
                runtime_state="running",
            )
            self.assertIs(screen.is_running, True)
            self.assertIn("bridge.example.com", screen.view()["endpoint"])


class FrameworkAttributeContractTests(unittest.TestCase):
    """Level-2 recurrence guard: a static AST contract, not a string grep.

    The defect returned twice because nothing stopped a developer from writing
    ``self._running = ...`` on a screen again. This parses the source and walks
    the AST, so a renamed helper or a doctored ``inspect.getsource`` cannot
    hide the assignment the way a substring assertion could.

    The contract is deliberately narrow -- the two assignments that are *known*
    to collide -- rather than a sweeping ban that would flag every legitimate
    private attribute Textual happens to define.
    """

    @staticmethod
    def _class_assignments(source: str, class_name: str) -> set[tuple[str, str]]:
        """Map ``self.<attr>`` names assigned inside one class, via AST.

        Returns a set of attribute names that receive ``self.<name> = ...`` so
        a caller can assert none of them are framework-reserved.
        """

        tree = ast.parse(source)
        assigned: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if (
                                isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"
                            ):
                                assigned.add(target.attr)
        return assigned

    def _source(self) -> str:
        path = Path(hub.__file__)
        return path.read_text(encoding="utf-8")

    def test_connection_detail_screen_does_not_assign_running(self) -> None:
        assigned = self._class_assignments(
            self._source(), "ConnectionDetailScreen"
        )
        self.assertNotIn(
            "_running",
            assigned,
            "ConnectionDetailScreen must not assign self._running; it shadows "
            "Textual MessagePump._running and breaks mount. Use _bridge_running.",
        )

    def test_connection_detail_screen_does_not_assign_name(self) -> None:
        # The earlier _name -> _display_name fix must not be undone.
        assigned = self._class_assignments(
            self._source(), "ConnectionDetailScreen"
        )
        self.assertNotIn(
            "_name",
            assigned,
            "ConnectionDetailScreen must not assign self._name; it shadows "
            "Widget._name. Use _display_name.",
        )

    def test_no_screen_or_widget_class_shadows_reserved_framework_attrs(self) -> None:
        """Every Screen/ModalScreen/Widget in both TUI modules, scanned once.

        A wide ban breaks on private attributes Textual defines for its own use
        without any real collision, so the reserved set is the two names with a
        *proven* mount-time effect plus the documented Textual lifecycle names
        that carry a type or meaning a connection field would corrupt.
        """

        reserved = {
            "_running",
            "_name",
            "_parent",
            "_mounted",
            "_closing",
            "_closed",
            "_disabled",
            "_id",
            "_classes",
            "_screen",
            "_app",
            "_message_queue",
        }
        widget_bases = {"Screen", "ModalScreen", "Widget", "App"}

        for module_path in (Path(tui.__file__), Path(hub.__file__)):
            source = module_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = {
                    base.id
                    for base in node.bases
                    if isinstance(base, ast.Name)
                }
                if not (bases & widget_bases):
                    continue
                assigned = self._class_assignments(source, node.name)
                collisions = assigned & reserved
                # _display_name is the sanctioned rename for _name and is fine.
                collisions.discard("_display_name")
                self.assertFalse(
                    collisions,
                    f"{module_path.name}:{node.name} assigns reserved framework "
                    f"attributes {sorted(collisions)}",
                )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
