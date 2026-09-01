from __future__ import annotations

import inspect

import karox.desktop_apps as desktop_apps


def test_desktop_background_contract_forbids_global_input_and_foreground() -> None:
    contract = desktop_apps._background_control_contract()
    assert contract["background_control"] is True
    assert contract["focus_steal_blocked"] is True
    assert contract["global_keyboard_injection"] is False
    assert contract["global_pointer_injection"] is False
    assert contract["screen_capture_mode"] == "on-demand"
    assert contract["background_polling"] is False
    source = inspect.getsource(desktop_apps)
    for forbidden in (
        "SetForegroundWindow(",
        "AttachThreadInput(",
        "SetCursorPos(",
        "mouse_event(",
        "keybd_event(",
        "SendInput(",
    ):
        assert forbidden not in source


def test_desktop_snapshot_backend_is_window_scoped_not_screen_grab() -> None:
    source = inspect.getsource(desktop_apps.DesktopApps.snapshot)
    assert "_capture_window_png" in source
    assert "ImageGrab.grab" not in source
    assert "bbox=" not in source


def test_desktop_module_has_no_background_capture_or_input_polling_loop() -> None:
    source = inspect.getsource(desktop_apps)
    for forbidden in (
        "time.sleep(",
        "threading.Thread(",
        "threading.Timer(",
        "while True:",
        "ImageGrab",
        "mss.",
        "pyautogui",
    ):
        assert forbidden not in source
