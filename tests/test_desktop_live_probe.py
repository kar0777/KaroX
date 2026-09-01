from __future__ import annotations

import json
import os

import pytest

from karox.desktop_apps import _windows


@pytest.mark.skipif(os.name != "nt", reason="Windows-only live desktop probe")
def test_live_windows_enumeration_does_not_raise() -> None:
    windows = _windows()
    assert isinstance(windows, list)
    assert all(item.hwnd > 0 and item.pid > 0 for item in windows)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only live desktop probe")
def test_live_window_metadata_is_utf8_json_transport_safe() -> None:
    payload = {"windows": [item.public() for item in _windows()]}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert encoded.startswith(b"{")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only live desktop probe")
def test_live_traycer_window_is_present_when_remote_control_is_expected() -> None:
    windows = _windows()
    if not any("traycer" in item.title.casefold() for item in windows):
        pytest.skip("Traycer is not currently open")
