from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from karox.desktop_apps import (
    DesktopAppSecurityError,
    Window,
    execute_desktop_app_action,
)
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.workspace_worker import execute_browser_command


class _Artifact:
    def to_dict(self) -> dict[str, object]:
        return {"artifact_id": "art-desktop", "mime": "image/png", "size": 3}


class _Artifacts:
    def put(self, data: bytes, *, name: str, mime: str) -> _Artifact:
        self.last = (data, name, mime)
        return _Artifact()


class DesktopAppTests(unittest.TestCase):
    def runtime(self, profile: AccessProfile = AccessProfile.ELEVATED) -> SimpleNamespace:
        origin = Origin(OriginKind.HOSTED_CLIENT, "desktop-test")
        policy = CapabilityPolicy(profile)
        policy.set_grants(origin, {Capability.BROWSER_READ, Capability.BROWSER_INPUT})
        return SimpleNamespace(
            _access_profile=profile,
            hosted_origin=origin,
            policy=policy,
            _artifacts=_Artifacts(),
        )

    @staticmethod
    def traycer(pid: int = 42) -> Window:
        return Window(1001, "Traycer - aqurium", (10, 20, 1210, 820), pid, False)

    def test_attach_traycer_opts_in_and_pins_window(self) -> None:
        runtime = self.runtime()
        with mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]):
            result = execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "access": "control", "user_confirmed": True},
                5.0,
            )
            status = execute_desktop_app_action(runtime, "app.status", {}, 5.0)
        self.assertTrue(result["attached"])
        self.assertEqual(result["window"]["process_id"], 42)
        self.assertEqual(status["bindings"][0]["app_id"], "traycer")
        contract = result["control_contract"]
        self.assertTrue(contract["background_control"])
        self.assertTrue(contract["focus_steal_blocked"])
        self.assertFalse(contract["global_keyboard_injection"])
        self.assertFalse(contract["global_pointer_injection"])
        self.assertEqual(contract["screen_capture_backend"], "win32-printwindow")
        self.assertTrue(runtime.policy.decide(runtime.hosted_origin, Capability.DESKTOP_INPUT).allowed)

    def test_attach_requires_explicit_confirmation_and_elevated_profile(self) -> None:
        with self.assertRaises(DesktopAppSecurityError):
            execute_desktop_app_action(
                self.runtime(), "app.attach", {"app_id": "traycer"}, 5.0
            )
        with self.assertRaises(PolicyDenied):
            execute_desktop_app_action(
                self.runtime(AccessProfile.WORKSPACE_WRITE),
                "app.attach",
                {"app_id": "traycer", "user_confirmed": True},
                5.0,
            )

    def test_pid_reuse_revokes_binding_before_input(self) -> None:
        runtime = self.runtime()
        with mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]):
            execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "user_confirmed": True},
                5.0,
            )
        reused = self.traycer(pid=99)
        with mock.patch("karox.desktop_apps._windows", return_value=[reused]):
            with self.assertRaises(DesktopAppSecurityError):
                execute_desktop_app_action(
                    runtime,
                    "app.type",
                    {"app_id": "traycer", "text": "continue"},
                    5.0,
                )
        self.assertEqual(runtime._desktop_apps.bindings, {})

    def test_observe_only_binding_blocks_input(self) -> None:
        runtime = self.runtime()
        with mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]):
            execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "access": "observe", "user_confirmed": True},
                5.0,
            )
            with self.assertRaises(DesktopAppSecurityError):
                execute_desktop_app_action(
                    runtime,
                    "app.click",
                    {"app_id": "traycer", "x": 20, "y": 20},
                    5.0,
                )

    def test_unicode_type_routes_without_echoing_prompt(self) -> None:
        runtime = self.runtime()
        prompt = "Продолжи работу над проектом"
        with (
            mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]),
            mock.patch("karox.desktop_apps._type_unicode") as send,
        ):
            execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "user_confirmed": True},
                5.0,
            )
            result = execute_desktop_app_action(
                runtime,
                "app.type",
                {"app_id": "traycer", "text": prompt},
                5.0,
            )
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], prompt)
        self.assertNotIn("text", result)
        self.assertEqual(result["text_length"], len(prompt))

    def test_workspace_worker_routes_app_action_before_browser_manager(self) -> None:
        runtime = self.runtime()
        with mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]):
            result = execute_browser_command(
                runtime,
                {
                    "action": "app.attach",
                    "payload": {"app_id": "traycer", "user_confirmed": True},
                },
                5.0,
            )
        self.assertEqual(result["action"], "app.attach")
        self.assertTrue(result["attached"])

    def test_focus_action_never_calls_win32_focus_mutation(self) -> None:
        runtime = self.runtime()
        with (
            mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]),
            mock.patch("karox.desktop_apps._focus") as focus,
        ):
            execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "user_confirmed": True},
                5.0,
            )
            result = execute_desktop_app_action(
                runtime, "app.focus", {"app_id": "traycer"}, 5.0
            )
        focus.assert_not_called()
        self.assertFalse(result["focused"])
        self.assertTrue(result["background_control"])
        self.assertTrue(result["focus_steal_blocked"])

    def test_restore_uses_nonactivating_window_show(self) -> None:
        runtime = self.runtime()
        with (
            mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]),
            mock.patch("karox.desktop_apps._restore_without_activation") as restore,
        ):
            execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "user_confirmed": True},
                5.0,
            )
            result = execute_desktop_app_action(
                runtime, "app.restore", {"app_id": "traycer"}, 5.0
            )
        restore.assert_called_once()
        self.assertTrue(result["restored"])
        self.assertFalse(result["activated"])
        self.assertTrue(result["focus_steal_blocked"])

    def test_launch_requires_confirmation_and_routes_only_through_guarded_shortcut_helper(self) -> None:
        runtime = self.runtime()
        with self.assertRaises(DesktopAppSecurityError):
            execute_desktop_app_action(
                runtime,
                "app.launch",
                {"shortcut_path": r"C:\\Start Menu\\Programs\\Example.lnk"},
                5.0,
            )

        with mock.patch(
            "karox.desktop_apps._launch_start_menu_shortcut",
            return_value={
                "launched": True,
                "shortcut_name": "BitBrowser Global.lnk",
                "activation_requested": False,
                "matched_window": None,
                "launch_contract": {
                    "arguments_allowed": False,
                    "start_menu_only": True,
                    "show_command": "SW_SHOWMINNOACTIVE",
                },
            },
        ) as launch:
            result = execute_desktop_app_action(
                runtime,
                "app.launch",
                {
                    "shortcut_path": r"C:\\Users\\u\\Start Menu\\Programs\\BitBrowser Global.lnk",
                    "title_contains": "BitBrowser",
                    "user_confirmed": True,
                },
                5.0,
            )
        launch.assert_called_once_with(
            r"C:\\Users\\u\\Start Menu\\Programs\\BitBrowser Global.lnk", "BitBrowser"
        )
        self.assertTrue(result["launched"])
        self.assertFalse(result["activation_requested"])
        self.assertFalse(result["launch_contract"]["arguments_allowed"])
        self.assertTrue(result["launch_contract"]["start_menu_only"])

    def test_snapshot_refuses_explicit_foreground_capture(self) -> None:
        runtime = self.runtime()
        with mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]):
            execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "user_confirmed": True},
                5.0,
            )
            with self.assertRaises(DesktopAppSecurityError):
                execute_desktop_app_action(
                    runtime,
                    "app.snapshot",
                    {"app_id": "traycer", "bring_to_front": True},
                    5.0,
                )

    def test_snapshot_uses_window_only_background_capture(self) -> None:
        runtime = self.runtime()
        with (
            mock.patch("karox.desktop_apps._windows", return_value=[self.traycer()]),
            mock.patch(
                "karox.desktop_apps._capture_window_png",
                return_value=(b"png", 1200, 800),
            ) as capture,
        ):
            execute_desktop_app_action(
                runtime,
                "app.attach",
                {"app_id": "traycer", "user_confirmed": True},
                5.0,
            )
            result = execute_desktop_app_action(
                runtime,
                "app.snapshot",
                {"app_id": "traycer"},
                5.0,
            )
        capture.assert_called_once_with(self.traycer())
        self.assertEqual(runtime._artifacts.last[0], b"png")
        self.assertEqual(result["capture_method"], "win32-printwindow")
        self.assertTrue(result["non_intrusive"])
        self.assertTrue(result["control_contract"]["focus_steal_blocked"])

    def test_desktop_control_contains_no_global_input_primitives(self) -> None:
        import inspect
        import karox.desktop_apps as desktop_apps

        source = inspect.getsource(desktop_apps)
        for forbidden in (
            "SetForegroundWindow(",
            "AttachThreadInput(",
            "SetCursorPos(",
            "mouse_event(",
            "keybd_event(",
            "SendInput(",
            "ImageGrab.grab(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
