"""Tracks E/G: real Textual navigation, secure workflow reuse, no live bridge.

Only external I/O (provider responses, OS keyring backend, bridge launches,
Tailscale status) is replaced. Progress and provider registries are real files
inside the repository's isolated test directories.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

from _unittest_compat import enter_context
from _tui_harness import isolated_karox_directories, settle_service_screen
from karox import onboarding as state
from karox import tui
from karox import tui_connections as connections
from karox import tui_onboarding as guide
from karox.credentials import CredentialStore
from karox.provider_controller import ProviderController
from karox.web_bridge_profiles import SavedWebBridgeProfile, WebBridgeProfileStore


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())

    def test_language_does_not_imply_guide_completed(self) -> None:
        tui._save_language("en")
        self.assertIsNone(state.load_progress())
        state.save_progress(state.OnboardingProgress())
        self.assertFalse(state.load_progress().completed)
        self.assertEqual(tui._load_language(), "en")

    def test_atomic_round_trip_and_only_finite_nonsecret_fields(self) -> None:
        progress = state.OnboardingProgress(step="tunnel", service="claude-web", tunnel="tailscale")
        state.save_progress(progress)
        self.assertEqual(state.load_progress(), progress)
        self.assertEqual(set(json.loads(state.progress_path().read_text())), {
            "version", "step", "completed", "service", "tunnel",
            "provider_outcome", "bridge_outcome", "guide_dismissed",
        })
        self.assertEqual(list(state.progress_path().parent.glob(".onboarding-*")), [])
        if os.name != "nt":
            self.assertEqual(state.progress_path().stat().st_mode & 0o777, 0o600)

    def test_invalid_or_future_state_is_not_completion(self) -> None:
        state.progress_path().parent.mkdir(parents=True)
        for value in ('{', '[]', '{"version":2}', '{"version":1,"completed":true}'):
            state.progress_path().write_text(value)
            self.assertIsNone(state.load_progress())
        for kwargs in ({"step": "api-key"}, {"completed": True}, {"service": "secret"}, {"tunnel": "secret"}):
            with self.assertRaises(ValueError):
                state.OnboardingProgress(**kwargs)

    def test_legacy_completion_is_dismissal_not_configuration(self) -> None:
        state.progress_path().parent.mkdir(parents=True)
        state.progress_path().write_text(json.dumps({
            "version": 1, "step": "review", "completed": True,
            "service": "chatgpt-web", "tunnel": "cloudflare",
        }))
        progress = state.load_progress()
        self.assertFalse(progress.completed)
        self.assertTrue(progress.guide_dismissed)

    def test_failed_replace_keeps_previous_step_and_cleans_temp(self) -> None:
        state.save_progress(state.OnboardingProgress())
        with patch.object(state.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                state.save_progress(state.OnboardingProgress(step="bridge"))
        self.assertEqual(state.load_progress().step, "provider")
        self.assertEqual(list(state.progress_path().parent.glob(".onboarding-*")), [])

    def test_one_time_tailscale_links_are_never_diagnostic_output(self) -> None:
        message = state.safe_setup_error("login https://login.tailscale.com/a/fixture-secret now")
        self.assertNotIn("fixture-secret", message)
        self.assertIn("hidden", message)


    def test_active_login_shows_manual_url_before_wait_when_browser_fails(self) -> None:
        # Active login is not a diagnostic: preserve the established manual
        # fallback, even when opening a browser fails on a headless machine.
        url = "https://login.tailscale.com/a/fixture-login"
        for failure in (False, OSError("no browser")):
            with self.subTest(browser=type(failure).__name__):
                app = Mock()
                app._label = lambda ru, en: en
                app.call_from_thread = lambda fn, *args: fn(*args)
                process = Mock()
                process.stdout = iter(["To authenticate, visit:\n", url + "\n"])

                def wait(*args, **kwargs):
                    self.assertTrue(any(url in str(c) for c in app._write.call_args_list),
                                    "Manual sign-in URL must be visible before login waits")
                    return 0

                process.wait.side_effect = wait
                with (
                    patch.object(tui.subprocess, "Popen", return_value=process),
                    patch.object(tui, "_tailscale_logged_in", return_value=False),
                    patch("webbrowser.open", side_effect=failure if isinstance(failure, Exception) else None,
                          return_value=False) as browser,
                ):
                    tui.KaroXApp._tailscale_login_worker(app, 8765, Mock(), "/usr/bin/tailscale")
                browser.assert_called_once_with(url)
                process.wait.assert_called_once_with(timeout=180)
                self.assertNotIn("fixture-login", state.safe_setup_error(url))


class HeadlessCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())

    def app(self, language="en") -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language=language)

    async def wait_screen(self, pilot, app, cls):
        for _ in range(100):
            if (type(app.screen).__name__ == cls) if isinstance(cls, str) else isinstance(app.screen, cls):
                await pilot.pause()
                return app.screen
            await pilot.pause(0.02)
        self.fail(f"expected {cls}, got {type(app.screen).__name__}")


class OnboardingFlowTests(HeadlessCase):
    async def test_completed_guide_is_not_offered_again_on_normal_start(self) -> None:
        tui._save_language("en")
        completed = state.OnboardingProgress(step="review", completed=True, bridge_outcome="configured", guide_dismissed=True)
        state.save_progress(completed)
        app = self.app(None)
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.pause()
            self.assertEqual(app.focused.id, "composer")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertNotIn("Setup pending", app.query_one("#conversation", tui.TranscriptView).plain_text)
            self.assertEqual(state.load_progress(), completed)

    async def test_first_language_offers_setup_without_stealing_chat(self) -> None:
        app = self.app(None)
        async with app.run_test(size=(46, 14)) as pilot:
            screen = await self.wait_screen(pilot, app, tui.LanguageScreen)
            self.assertLessEqual(screen.query_one("#language-dialog").size.width, 46)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.focused.id, "composer")
            self.assertEqual(state.load_progress().step, "provider")
            self.assertFalse(state.load_progress().completed)
            await pilot.press("f4")
            await self.wait_screen(pilot, app, guide.FirstRunScreen)

    async def test_existing_users_are_not_migrated_or_prompted(self) -> None:
        tui._save_language("ru")
        app = self.app(None)
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.pause()
            self.assertEqual(app.focused.id, "composer")
            self.assertIsNone(state.load_progress())
            self.assertNotIn("F4", app.query_one("#conversation", tui.TranscriptView).plain_text)

    async def test_keyboard_skip_pause_resume_finish_and_no_completion_on_escape(self) -> None:
        app = self.app()
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.press("f4")
            await self.wait_screen(pilot, app, guide.FirstRunScreen)
            await pilot.press("down", "space")  # Skip API.
            screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
            self.assertEqual(screen.progress.step, "bridge")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(state.load_progress().completed)
            self.assertEqual(app.focused.id, "composer")
        app = self.app()
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.press("f4")
            screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
            self.assertEqual(screen.progress.step, "bridge")
            await pilot.press("down", "down", "enter")  # Skip bridge.
            screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
            self.assertEqual(screen.progress.step, "review")
            self.assertFalse(state.load_progress().completed)
            await pilot.press("enter")
            await pilot.pause()
            self.assertFalse(state.load_progress().completed)
            self.assertTrue(state.load_progress().guide_dismissed)
            self.assertEqual(state.load_progress().bridge_outcome, "skipped")
            self.assertEqual(app.focused.id, "composer")

    async def test_real_provider_wizard_uses_keyring_controller_not_progress_file(self) -> None:
        backend = Mock()
        secrets = {}
        backend.set.side_effect = lambda service, account, secret: secrets.update({account: secret})
        backend.get.side_effect = lambda service, account: secrets.get(account)
        controller = ProviderController(registry=tui._registry(), credentials=CredentialStore(backend))
        secret = "sk-test-onboarding-not-a-real-key"
        discovery = tui.ModelDiscovery((tui.DiscoveredModel("fixture-model"),), "https://api.openai.com/v1", ())
        with (
            patch.object(tui, "_provider_controller", return_value=controller),
            patch.object(tui, "_discover_models_result", return_value=discovery),
            patch.object(tui, "_probe_provider", return_value={}),
            patch.object(connections, "discover_models_for_provider", return_value=()),
        ):
            app = self.app()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.press("f4", "enter")
                await self.wait_screen(pilot, app, tui.ProviderPresetScreen)
                await pilot.press(*list("openai"), "down", "down", "enter")
                provider = await self.wait_screen(pilot, app, tui.ProviderSetupScreen)
                self.assertTrue(provider.query_one("#provider-key", tui.Input).password)
                provider.query_one("#provider-key", tui.Input).value = secret
                await pilot.press("f5")
                await self.wait_screen(pilot, app, tui.ModelPickerScreen)
                await pilot.press("enter")
                screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
                self.assertEqual(screen.progress.step, "bridge")
                self.assertEqual(tui._selected_model().model_id, "fixture-model")
                self.assertIn(secret, secrets.values())
                for file in (state.progress_path(), tui._preferences_path(), tui._registry().path):
                    if file.exists():
                        self.assertNotIn(secret, file.read_text())
                self.assertNotIn(secret, app.query_one("#conversation", tui.TranscriptView).plain_text)

    async def test_provider_cancel_returns_to_pending_step(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("f4", "enter")
            await self.wait_screen(pilot, app, tui.ProviderPresetScreen)
            await pilot.press("escape")
            screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
            self.assertEqual(screen.progress.step, "provider")
            self.assertFalse(state.load_progress().completed)

    async def test_chatgpt_cloudflare_reuses_real_locked_setup_and_cancel_resumes(self) -> None:
        state.save_progress(state.OnboardingProgress(step="bridge"))
        app = self.app()
        with patch.object(app, "_bridge_setup_done") as launch:
            async with app.run_test(size=(46, 14)) as pilot:
                await pilot.press("f4", "enter")
                screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
                self.assertEqual(screen.progress.step, "tunnel")
                await pilot.press("enter")
                form = await self.wait_screen(pilot, app, tui.BridgeSetupScreen)
                self.assertEqual(form._profile_value(), "chatgpt-web")
                self.assertEqual(form._tunnel_value(), "cloudflare")
                launch.assert_not_called()
                await pilot.press("escape")
                await self.wait_screen(pilot, app, guide.FirstRunScreen)
                self.assertEqual(state.load_progress().step, "tunnel")
                launch.assert_not_called()

    async def test_claude_tunnel_start_reaches_existing_service_screen_no_false_completion(self) -> None:
        state.save_progress(state.OnboardingProgress(step="tunnel", service="claude-web"))
        app = self.app()
        with patch.object(app, "_bridge_setup_done") as launch:
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.press("f4", "enter")
                await self.wait_screen(pilot, app, tui.BridgeSetupScreen)
                await pilot.press("f10")
                service = await self.wait_screen(pilot, app, app._connections_screens_cached()["ServiceConnectScreen"])
                self.assertEqual(service.preset_id, "claude-web")
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0].tunnel_provider, "cloudflare")
                self.assertFalse(state.load_progress().completed)
                await pilot.press("escape")
                screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
                self.assertEqual(screen.progress.step, "review")
                self.assertFalse(state.load_progress().completed)

    async def test_requested_failed_bridge_cannot_finish_even_via_callback(self) -> None:
        state.save_progress(state.OnboardingProgress(step="tunnel", bridge_outcome="requested"))
        app = self.app()
        async with app.run_test(size=(80, 30)) as pilot:
            app._first_run_bridge_launch_profile = "fixture"
            app._saved_bridge_from_tui_done("fixture", {"action": "error"}, "fixture launch failure")
            # Even a stale working screen must not override this launch failure.
            app._first_run_service_closed(None, service_screen=Mock(_status=connections.SERVICE_WORKING))
            screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
            self.assertTrue(screen.query_one("#setup-finish", tui.Button).disabled)
            await pilot.press("escape")
            app._first_run_choice("setup-finish")
            await pilot.pause()
            progress = state.load_progress()
            self.assertFalse(progress.completed)
            self.assertFalse(progress.guide_dismissed)
            self.assertEqual(progress.bridge_outcome, "requested")
            await pilot.press("escape", "f4")
            await self.wait_screen(pilot, app, guide.FirstRunScreen)
            self.assertEqual(state.load_progress().bridge_outcome, "requested")

    async def test_verified_bridge_can_complete(self) -> None:
        state.save_progress(state.OnboardingProgress(step="tunnel", bridge_outcome="requested"))
        app = self.app()
        async with app.run_test(size=(80, 30)) as pilot:
            app._first_run_bridge_launch_profile = "fixture"
            app._saved_bridge_from_tui_done("fixture", {"action": "started"}, None)
            app._first_run_service_closed(None, service_screen=Mock(_status=connections.SERVICE_WORKING))
            await self.wait_screen(pilot, app, guide.FirstRunScreen)
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(state.load_progress().completed)
            self.assertTrue(state.load_progress().guide_dismissed)
            self.assertEqual(state.load_progress().bridge_outcome, "configured")

    async def test_direct_tunnel_radio_change_cannot_bypass_setup_consent(self) -> None:
        state.save_progress(state.OnboardingProgress(step="tunnel"))
        app = self.app()
        with patch.object(app, "_bridge_setup_done") as launch:
            async with app.run_test() as pilot:
                await pilot.press("f4", "enter")
                form = await self.wait_screen(pilot, app, tui.BridgeSetupScreen)
                form._tunnel_help_done("tailscale")
                await pilot.press("f10")
                await self.wait_screen(pilot, app, guide.TailscaleSetupScreen)
                launch.assert_not_called()
                await pilot.press("escape")
                await self.wait_screen(pilot, app, guide.FirstRunScreen)
                launch.assert_not_called()

    async def test_guide_f4_does_not_stack_over_a_password_form(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            app.push_screen(tui.ProviderSetupScreen("en", tui.provider_preset("openai")))
            await pilot.pause()
            depth = len(app.screen_stack)
            await pilot.press("f4")
            self.assertEqual(len(app.screen_stack), depth)

    async def test_ru_en_narrow_guide_navigation_footer_and_help(self) -> None:
        for language in ("ru", "en"):
            for step in state.STEPS:
                with self.subTest(language=language, step=step):
                    state.save_progress(state.OnboardingProgress(step=step))
                    app = self.app(language)
                    async with app.run_test(size=(40, 12)) as pilot:
                        await pilot.press("f4")
                        screen = await self.wait_screen(pilot, app, guide.FirstRunScreen)
                        self.assertLessEqual(screen.query_one(".guide-dialog").size.width, 40)
                        self.assertTrue(screen.query_one(".guide-footer").is_on_screen)
                        # Every action can be focused and scrolled into view.
                        for _ in range(len(screen.query(tui.Button))):
                            self.assertTrue(app.focused.is_on_screen)
                            await pilot.press("tab")
                        await pilot.press("f1")
                        await self.wait_screen(pilot, app, guide.NavigationHelpScreen)
                        depth = len(app.screen_stack)
                        await pilot.press("f1")
                        self.assertEqual(len(app.screen_stack), depth)
                        await pilot.press("escape")
                        self.assertIs(app.screen, screen)


class TailscaleAndBypassTests(HeadlessCase):
    # Separate methods below share the headless helpers, not external services.
    async def test_tailscale_check_is_read_only_probes_once_and_no_status_leaks(self) -> None:
        app = self.app()
        result = {"ready": False, "code": "login_required", "auth_url": "https://login.tailscale.com/a/fixture-secret", "detail": "fixture-secret"}
        with patch.object(guide, "query_tailscale_status", return_value=result) as probe:
            async with app.run_test(size=(46, 14)) as pilot:
                screen = guide.TailscaleSetupScreen("en")
                app.push_screen(screen)
                await pilot.pause()
                probe.assert_not_called()  # Opening alone never probes or mutates.
                self.assertTrue(screen.query_one("#tailscale-use", tui.Button).disabled)
                await pilot.press("f5")
                for _ in range(100):
                    if not screen._checking:
                        break
                    await pilot.pause(0.02)
                probe.assert_called_once_with(executable=None)
                self.assertNotIn("fixture-secret", app.export_screenshot())
                self.assertIn("Sign in", str(screen.query_one("#tailscale-status", tui.Static).render()))
                self.assertTrue(screen.query_one("#tailscale-use", tui.Button).disabled)

    async def test_tailscale_requires_explicit_allow_then_preserves_choice(self) -> None:
        state.save_progress(state.OnboardingProgress(step="tunnel", service="claude-web"))
        app = self.app()
        with patch.object(guide, "query_tailscale_status", return_value={"ready": True, "code": "ready"}):
            async with app.run_test(size=(46, 14)) as pilot:
                await pilot.press("f4", "down", "enter")
                screen = await self.wait_screen(pilot, app, guide.TailscaleSetupScreen)
                await pilot.press("f5")
                for _ in range(100):
                    if not screen._checking:
                        break
                    await pilot.pause(0.02)
                self.assertIs(app.screen, screen)  # Readiness is not consent.
                await pilot.press("tab", "enter")
                form = await self.wait_screen(pilot, app, tui.BridgeSetupScreen)
                self.assertEqual(form._tunnel_value(), "tailscale")
                self.assertEqual(form._profile_value(), "claude-web")

    async def test_tailscale_install_offer_needs_double_consent_and_downloads_once(self) -> None:
        from karox.tailscale_bootstrap import TailscaleAsset

        state.save_progress(state.OnboardingProgress(step="tunnel", service="chatgpt-web"))
        app = self.app()
        asset = TailscaleAsset("tailscale_1.102.4_amd64.tgz", "a" * 64, "tgz")
        downloads = []
        with (
            patch.object(guide, "tailscale_asset", return_value=asset),
            patch.object(
                guide,
                "ensure_tailscale_downloads",
                side_effect=lambda **kwargs: downloads.append(kwargs)
                or {"tailscale": "/fixture/ts", "tailscaled": "/fixture/tsd"},
            ),
            patch.object(guide.subprocess, "Popen") as popen,
        ):
            async with app.run_test(size=(60, 20)) as pilot:
                screen = guide.TailscaleSetupScreen("en")
                app.push_screen(screen)
                await pilot.pause()
                # First press: consent details only; nothing downloads or runs.
                screen.query_one("#tailscale-install", tui.Button).press()
                await pilot.pause()
                popen.assert_not_called()
                self.assertEqual(downloads, [])
                status = str(screen.query_one("#tailscale-status", tui.Static).render())
                self.assertIn("SHA-256", status)
                # Second press: the verified download runs; Linux unpacks
                # user-owned binaries and prints the official repo commands.
                screen.query_one("#tailscale-install", tui.Button).press()
                # The worker sets `_installing` and then downloads on its own
                # thread, so the flag alone can still read False before the
                # worker started — that returned immediately and reported
                # "0 downloads" on a loaded runner. Wait for the download the
                # assertion is about, bounded, and still fail if it never comes.
                for _ in range(150):
                    if downloads and not screen._installing:
                        break
                    await pilot.pause(0.02)
                await pilot.pause()
                popen.assert_not_called()
                self.assertEqual(len(downloads), 1)
                status = str(screen.query_one("#tailscale-status", tui.Static).render())
                self.assertIn("/fixture/ts", status)
                self.assertIn("sudo apt-get install -y tailscale", status)

    async def test_tailscale_cloudflare_fallback_needs_no_status_check(self) -> None:
        app = self.app()
        choices = []
        with patch.object(guide, "query_tailscale_status") as probe:
            async with app.run_test(size=(46, 14)) as pilot:
                app.push_screen(guide.TailscaleSetupScreen("ru"), choices.append)
                await pilot.pause()
                await pilot.press("tab", "space")  # disabled Allow is skipped.
                await pilot.pause()
                self.assertEqual(choices, ["cloudflare"])
                probe.assert_not_called()

    async def test_bypass_space_and_enter_work_through_existing_confirmation(self) -> None:
        for language in ("en", "ru"):
            with self.subTest(language=language):
                base = SavedWebBridgeProfile(
                    name="eg-shared", target_profile="chatgpt-web", repository=str(self.repository),
                    tools=("karox.repo.read_file",), tunnel="custom", public_url="https://bridge.example.test", port=8765,
                )
                store = WebBridgeProfileStore()
                store.put(base)
                app = self.app(language)
                async with app.run_test(size=(46, 14)) as pilot:
                    screen = app._connections_screens_cached()["ServiceConnectScreen"](language, preset_id="chatgpt-web")
                    app.push_screen(screen)
                    await pilot.pause()
                    await settle_service_screen(screen, pilot)
                    await pilot.press("b")
                    switch = screen.query_one("#svc-full-access", connections.Switch)
                    self.assertIs(app.focused, switch)
                    self.assertFalse(switch.value)
                    with patch.object(screen, "_apply_bypass") as apply:
                        await pilot.press("space")
                        await self.wait_screen(pilot, app, "_ConfirmScreen")
                        apply.assert_not_called()
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertFalse(switch.value)
                        apply.assert_not_called()
                        await pilot.press("b", "enter")
                        await self.wait_screen(pilot, app, "_ConfirmScreen")
                        # Explicitly accept through the actual focused confirm button.
                        await pilot.press("enter")
                        await pilot.pause()
                        apply.assert_called_once()
