"""TUI navigation and non-blocking behaviour for the new Connections screens.

These boot the real ``KaroXApp`` with isolated config/runtime directories and
drive the Connections hub with the Pilot, mirroring the existing TUI test
conventions in ``test_tui_*.py``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

import karox.tui as tui
import karox.clickup_setup as clickup_setup
from karox.connections import (
    ConnectionCredentialStore,
    build_target_from_preset,
    connection_registry,
    resolve_clickup_defaults,
)
from karox.connection_runtime import connection_runtime_manager


class _FakeBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service, account, secret) -> None:
        self.store[(service, account)] = secret

    def get(self, service, account):
        return self.store.get((service, account))

    def delete(self, service, account) -> None:
        self.store.pop((service, account), None)


def _seed_clickup_connection(backend: _FakeBackend) -> str:
    """Seed a saved ClickUp connection under isolated config dirs."""
    store = ConnectionCredentialStore(backend=backend)
    info = store.set("clickup-demo", "sek-1234567890")
    target = build_target_from_preset(
        "clickup",
        name="My ClickUp",
        credential_ref=info["reference"],
        credential_fingerprint=info["fingerprint"],
    )
    connection_registry().put(target)
    return target.connection_id


class ConnectionsTuiTests(unittest.IsolatedAsyncioTestCase):
    async def _app(self, *, language: str = "en"):
        from karox.tui_connections import build_connections_screens

        self._ctx = isolated_karox_directories()
        repository = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        app = tui.KaroXApp(repository, language=language)
        async with app.run_test(size=(120, 42)) as pilot:
            # Build + cache the screens so the isinstance checks below match the
            # classes that end up on the screen stack.
            app._connections_screens = build_connections_screens(app)
            await pilot.pause(0.3)
            yield app, pilot

    async def test_hub_opens_and_routes_to_mcp_clients(self) -> None:
        async for app, pilot in self._app():
            screens = app._connections_screens
            app.push_screen(
                screens["ConnectionHubScreen"](language="en"),
                app._connection_hub_done,
            )
            await pilot.pause(0.3)
            hub = app.screen_stack[-1]
            self.assertEqual(hub.__class__.__name__, "ConnectionHubScreen")
            # Choose MCP clients.
            await pilot.press("1")
            await pilot.pause(0.4)
            mcp_cls = screens["McpClientsScreen"]
            self.assertTrue(any(isinstance(s, mcp_cls) for s in app.screen_stack))
            # Escape back out of the MCP clients list, then the hub.
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_mcp_clients_list_shows_saved_connection_and_secret_is_masked(self) -> None:
        backend = _FakeBackend()
        async for app, pilot in self._app():
            _seed_clickup_connection(backend)
            screens = app._connections_screens
            app.push_screen(screens["McpClientsScreen"](language="en"))
            await pilot.pause(0.3)
            mcp_screen = app.screen_stack[-1]
            options = mcp_screen.query_one("#mcp-connections", tui.OptionList)
            self.assertEqual(len(options.options), 1)
            # The rendered row must not contain the raw secret.
            from textual.widgets.option_list import Option as _Opt

            row = mcp_screen.query_one("#mcp-connections", tui.OptionList).get_option_at_index(0).prompt
            rendered = str(row)
            self.assertNotIn("sek-1234567890", rendered)
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_preset_picker_navigation_and_cancel(self) -> None:
        async for app, pilot in self._app():
            screens = app._connections_screens
            app.push_screen(screens["McpClientsScreen"](language="en"))
            await pilot.pause(0.3)
            # Open the add preset picker.
            await pilot.press("a")
            await pilot.pause(0.3)
            preset_screen = next(
                (s for s in app.screen_stack if s.__class__.__name__ == "_PresetPickerScreen"),
                None,
            )
            self.assertIsNotNone(preset_screen)
            options = preset_screen.query_one("#preset-pick-list", tui.OptionList)
            self.assertGreater(len(options.options), 0)
            # Arrow down then up exercises keyboard navigation.
            await pilot.press("down")
            await pilot.pause(0.1)
            await pilot.press("up")
            await pilot.pause(0.1)
            # Cancel back out.
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_connection_test_does_not_block_the_tui(self) -> None:
        # A test against an unreachable endpoint must return (on a worker thread)
        # without freezing the UI. We patch the test to a fast-failing stub so
        # the assertion is about non-blocking, not network state.
        backend = _FakeBackend()
        async for app, pilot in self._app():
            _seed_clickup_connection(backend)
            screens = app._connections_screens
            app.push_screen(screens["McpClientsScreen"](language="en"))
            await pilot.pause(0.3)
            mcp_screen = app.screen_stack[-1]

            import karox.connection_tests as ct

            called = {"n": 0}

            def _stub(target, *, endpoint_url, secret, timeout_seconds=15.0):
                called["n"] += 1
                import time as _t

                _t.sleep(0.05)
                return {"state": "failed", "failure_kind": "network", "detail": "stub"}

            # The controller owns registry lookup and secret dispatch. Save the
            # local fixture before selecting it, then patch the controller-level
            # resolver; an unsaved object must not be testable by presentation
            # code because it has no durable connection identity.
            local_target = build_target_from_preset(
                "generic-mcp",
                name="local",
                tunnel="local",
                credential_ref="os-keyring:connection/x",
                credential_fingerprint="sha256:abc",
            )
            mcp_screen._controller.registry.put(local_target)
            with patch.object(ct, "test_mcp_client_target", _stub), patch(
                "karox.connection_controller.resolve_connection_secret",
                return_value="stub-secret",
            ):
                mcp_screen._selected = lambda: local_target
                mcp_screen.action_test()
                # The UI must remain responsive while the worker runs: pressing
                # escape should not raise or deadlock.
                await pilot.pause(0.1)
                await pilot.press("escape")
                await pilot.pause(0.3)
            self.assertEqual(called["n"], 1)
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_saved_connection_row_shows_runtime_and_stop_works(self) -> None:
        backend = _FakeBackend()
        async for app, pilot in self._app():
            connection_id = _seed_clickup_connection(backend)
            stopped: list[str] = []
            connection_runtime_manager().register(
                connection_id=connection_id,
                session_id="clickup-tui-runtime",
                tunnel="cloudflare",
                local_endpoint="http://127.0.0.1:8765/mcp",
                public_endpoint="https://example.test/mcp",
                bridge_pid=None,
                tunnel_pid=None,
                stop=lambda: stopped.append("yes"),
            )
            screens = app._connections_screens
            app.push_screen(screens["McpClientsScreen"](language="en"))
            await pilot.pause(0.3)
            screen = app.screen_stack[-1]
            options = screen.query_one("#mcp-connections", tui.OptionList)
            self.assertIn("[running]", str(options.get_option_at_index(0).prompt))
            screen.action_stop()
            await pilot.pause(0.2)
            self.assertEqual(stopped, ["yes"])
            self.assertIn("[stopped]", str(options.get_option_at_index(0).prompt))
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_stopped_clickup_can_start_from_the_saved_connections_list(self) -> None:
        backend = _FakeBackend()
        async for app, pilot in self._app():
            connection_id = _seed_clickup_connection(backend)
            target = connection_registry().get(connection_id)
            called: list[str] = []

            def fake_start(saved_target):
                called.append(saved_target.connection_id)
                manager = connection_runtime_manager()
                manager.register(
                    connection_id=saved_target.connection_id,
                    session_id="clickup-tui-start",
                    tunnel="cloudflare",
                    local_endpoint="http://127.0.0.1:8765/mcp",
                    public_endpoint="https://example.test/mcp",
                    bridge_pid=None,
                    tunnel_pid=None,
                    stop=lambda: None,
                )
                return clickup_setup.ClickupSetupOutcome(
                    success=True,
                    target=saved_target,
                    public_endpoint="https://example.test/mcp",
                    local_endpoint="http://127.0.0.1:8765/mcp",
                    handshake={"state": "ok", "detail": "ok"},
                    defaults=resolve_clickup_defaults(
                        {"cloudflared_installed": True}
                    ),
                    stop=lambda: manager.stop(saved_target.connection_id),
                )

            screens = app._connections_screens
            app.push_screen(screens["McpClientsScreen"](language="en"))
            await pilot.pause(0.3)
            screen = app.screen_stack[-1]
            # Start/Restart is owned by ConnectionController now. Patch its
            # launcher and credential seam rather than a presentation-level
            # ClickUp function, which the TUI no longer calls directly.
            with patch.object(screen._controller, "_launcher", fake_start), patch.object(
                screen._controller,
                "_secret_resolver",
                lambda _target: "sek-1234567890",
            ):
                await pilot.press("r")
                await pilot.pause(0.8)
            self.assertEqual(called, [target.connection_id])
            self.assertEqual(
                connection_runtime_manager().status(target.connection_id)["state"],
                "running",
            )
            options = screen.query_one("#mcp-connections", tui.OptionList)
            self.assertIn("[running]", str(options.get_option_at_index(0).prompt))
            # A result card may be mounted depending on Textual callback timing;
            # the contract is the managed runtime and refreshed saved row.
            if app.screen.__class__.__name__ == "_ClickupResultScreen":
                await pilot.press("escape")
                await pilot.pause(0.2)
            connection_runtime_manager().stop(target.connection_id)
            if app.screen is screen:
                await pilot.press("escape")
                await pilot.pause(0.2)

    async def test_model_providers_screen_lists_saved_provider(self) -> None:
        async for app, pilot in self._app():
            # Seed a provider through the existing registry so the list is populated.
            from karox.paths import config_dir
            from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry

            reg = ProviderRegistry(config_dir() / "vnext" / "providers.json")
            reg.put_provider(
                ProviderRecord(
                    provider_id="openai",
                    adapter_kind="openai_compatible_chat",
                    base_url="https://api.openai.com/v1",
                )
            )
            reg.put_model(ModelRecord("openai", "gpt-test", tools="true"))
            screens = app._connections_screens
            app.push_screen(screens["ModelProvidersScreen"](language="en"))
            await pilot.pause(0.3)
            mp_screen = app.screen_stack[-1]
            options = mp_screen.query_one("#mp-providers", tui.OptionList)
            self.assertEqual(len(options.options), 1)
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_connect_slash_command_opens_hub(self) -> None:
        async for app, pilot in self._app():
            composer = app.query_one("#composer", tui.CommandInput)
            composer.value = "/connections"
            await pilot.press("enter")
            await pilot.pause(0.4)
            hub_open = any(
                s.__class__.__name__ == "ConnectionHubScreen" for s in app.screen_stack
            )
            self.assertTrue(hub_open, "/connections did not open the Connections hub")
            # Cancel back to the composer.
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_mcp_client_form_saves_preset_with_selected_auth(self) -> None:
        # Regression: every ``_selected_*()`` reader used ``cast(Any, radioset)[i]``,
        # which raises ``TypeError: 'RadioSet' object is not subscriptable``. The
        # transport/stability readers had no try/except, so pressing F10 to save a
        # preset form crashed before anything reached the registry. ClickUp now
        # routes to the auto-setup screen, so this exercises a preset that still
        # uses the manual form (generic-mcp) -- picker -> generic -> form -> F10 --
        # and asserts a connection is persisted with the auth scheme the user
        # actually selected.
        backend = _FakeBackend()
        async for app, pilot in self._app():
            screens = app._connections_screens
            app.push_screen(screens["McpClientsScreen"](language="en"))
            await pilot.pause(0.3)
            # Preset picker.
            await pilot.press("a")
            await pilot.pause(0.3)
            picker = next(
                (s for s in app.screen_stack if s.__class__.__name__ == "_PresetPickerScreen"),
                None,
            )
            self.assertIsNotNone(picker)
            options = picker.query_one("#preset-pick-list", tui.OptionList)
            while getattr(options.get_option_at_index(options.highlighted), "id", None) != "generic-mcp":
                await pilot.press("down")
                await pilot.pause(0.05)
            await pilot.press("enter")
            await pilot.pause(0.3)
            form = next(
                (s for s in app.screen_stack if s.__class__.__name__ == "_McpClientFormScreen"),
                None,
            )
            self.assertIsNotNone(form, "generic-mcp preset did not open the form")
            # Stub the credential store so the form uses the in-memory backend,
            # not the real OS keyring (which may be absent in CI/headless runs).
            form_store = ConnectionCredentialStore(backend=backend)
            with patch("karox.tui_connections.ConnectionCredentialStore", lambda *a, **k: form_store):
                form.query_one("#mcf-name", tui.Input).value = "My ClickUp"
                form.query_one("#mcf-secret", tui.Input).value = "sek-9999"
                # Select API key auth (down to index 1, then space) so we can
                # assert the reader honors the pressed button, not the default.
                auth = form.query_one("#mcp-form-auth", tui.RadioSet)
                auth.focus()
                await pilot.press("down")
                await pilot.pause(0.05)
                await pilot.press("space")
                await pilot.pause(0.05)
                self.assertEqual(auth.pressed_index, 1)
                # F10 must save instead of raising TypeError.
                await pilot.press("f10")
                await pilot.pause(0.4)
            # The form should have dismissed; a connection should be persisted.
            form_still_open = any(
                s.__class__.__name__ == "_McpClientFormScreen" for s in app.screen_stack
            )
            self.assertFalse(form_still_open, "form stayed open -- F10 save failed")
            saved = connection_registry().list()
            self.assertEqual(len(saved), 1)
            target = saved[0]
            self.assertEqual(target.preset_id, "generic-mcp")
            self.assertEqual(target.name, "My ClickUp")
            self.assertEqual(target.auth_scheme, "api_key")
            # The secret landed in the (fake) keyring, not the JSON config.
            self.assertTrue(target.credential_ref.startswith("os-keyring:connection/"))
            self.assertEqual(form_store.resolve(target.credential_ref), "sek-9999")
            await pilot.press("escape")
            await pilot.pause(0.2)

    async def test_escape_closes_the_connections_hub_modal(self) -> None:
        # Regression: the app's priority ``escape``->``stop_agent`` binding
        # swallowed the key before any modal's own escape binding could run, so
        # every Connections modal was impossible to close with Esc. The
        # ``check_action`` gate disables that binding while a modal is open and
        # the agent is idle, so Esc dismisses the modal instead.
        async for app, pilot in self._app():
            self.assertFalse(app.agent_busy)
            screens = app._connections_screens
            app.push_screen(screens["ConnectionHubScreen"](language="en"))
            await pilot.pause(0.3)
            self.assertEqual(app.screen.__class__.__name__, "ConnectionHubScreen")
            await pilot.press("escape")
            await pilot.pause(0.2)
            # Back at the root screen -- the modal was dismissed, not swallowed.
            self.assertEqual(app.screen.__class__.__name__, "Screen")


if __name__ == "__main__":
    unittest.main()
