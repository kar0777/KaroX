"""Deterministic contracts for the non-intrusive computer-use surface.

Everything here runs against the in-memory fake probe: no real window is
touched, no input reaches the user's session, and the same assertions hold on
any platform. Live ctypes coverage is gated separately in
``test_computer_use_live.py``.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from _computer_use_fake import (
    WM_CHAR,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_RBUTTONDOWN,
    WS_DISABLED,
    WS_VISIBLE,
    FakeProbe,
    FakeRuntime,
    FakeWindow,
)

from karox.computer_use import input as input_module
from karox.computer_use import launch as launch_module
from karox.computer_use import pngtools
from karox.computer_use import redact
from karox.computer_use.adapter import (
    ACTION_KINDS,
    CONSEQUENTIAL_ACTIONS,
    TOOL_SPECS,
    ComputerUseAdapter,
    ToolResult,
    action_digest,
    is_consequential,
)
from karox.computer_use.approval import ApprovalRejected, ApprovalStore
from karox.computer_use.deadline import Deadline
from karox.computer_use.errors import (
    ApprovalRequiredError,
    ComputerUseError,
    DesktopSecurityError,
    DesktopTimeoutError,
    InvalidActionError,
    TargetLostError,
)
from karox.computer_use.identity import WindowIdentity
from karox.computer_use.input import parse_key
from karox.computer_use.session import DesktopSession

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def make_adapter(tmp_path: Path, probe: FakeProbe, session: str = "unit") -> ComputerUseAdapter:
    return ComputerUseAdapter(
        runtime_root=tmp_path / "runtime",
        session_id=session,
        probe=probe,
    )


def sample_window(probe: FakeProbe, hwnd: int = 1001, pid: int = 4242) -> FakeWindow:
    window = FakeWindow(
        hwnd,
        "KaroX Test Editor - doc.txt",
        "Notepad",
        (100, 100, 900, 700),
        pid,
    )
    probe.add_window(window)
    return window


# --------------------------------------------------------------------------
# non-intrusion source contract
# --------------------------------------------------------------------------

FORBIDDEN_APIS = (
    "SetForegroundWindow",
    "AllowSetForegroundWindow",
    "SendInput",
    "keybd_event",
    "mouse_event",
    "SetWindowsHookEx",
    "AttachThreadInput",
    "BlockInput",
    "RegisterHotKey",
    "SetCursorPos",
    "SwapMouseButton",
    "GetAsyncKeyState",
    "LockSetForegroundWindow",
    "SetWinEventHook",
    "JournalRecord",
)


def test_no_global_injection_or_focus_stealing_apis_anywhere() -> None:
    """The package must never touch global input state or steal foreground.

    Background control means window-message targeting only. This scans the
    shipped source so a future edit cannot quietly reintroduce a global
    injection or interception call.
    """
    targets = list((SRC_ROOT / "karox" / "computer_use").rglob("*.py"))
    targets.append(SRC_ROOT / "karox" / "desktop_apps.py")
    assert targets, "computer_use package missing"
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for api in FORBIDDEN_APIS:
            assert api not in text, f"{path.name} references forbidden API {api}"


# --------------------------------------------------------------------------
# errors / deadline
# --------------------------------------------------------------------------


def test_error_hierarchy_is_typed() -> None:
    for cls in (
        DesktopSecurityError,
        DesktopTimeoutError,
        TargetLostError,
        InvalidActionError,
        ApprovalRequiredError,
    ):
        assert issubclass(cls, ComputerUseError)
    assert issubclass(ComputerUseError, RuntimeError)


def test_deadline_expires_with_fake_clock() -> None:
    clock = [1000.0]
    deadline = Deadline(lambda: clock[0], clock[0] + 2.0)
    assert not deadline.expired
    assert deadline.remaining() == pytest.approx(2.0)
    clock[0] += 1.0
    deadline.check()
    clock[0] += 1.5
    assert deadline.expired
    with pytest.raises(DesktopTimeoutError):
        deadline.check()


def test_deadline_after_uses_monotonic() -> None:
    deadline = Deadline.after(0.5)
    assert 0.0 < deadline.remaining() <= 0.5
    expired = Deadline.after(-1.0)
    assert expired.expired


# --------------------------------------------------------------------------
# stdlib PNG pipeline
# --------------------------------------------------------------------------


def _decode_png_chunks(png: bytes) -> dict[bytes, list[bytes]]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    chunks: dict[bytes, list[bytes]] = {}
    offset = 8
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])
        assert zlib.crc32(kind + data) & 0xFFFFFFFF == crc, f"bad CRC in {kind!r}"
        chunks.setdefault(kind, []).append(data)
        offset += 12 + length
    return chunks


def test_encode_png_roundtrips_exact_pixels() -> None:
    width, height = 3, 2
    # BGRX rows, top-down: red, green, blue / white, black, gray
    rows = (
        bytes((0, 0, 255, 255)) + bytes((0, 255, 0, 255)) + bytes((255, 0, 0, 255))
        + bytes((255, 255, 255, 255)) + bytes((0, 0, 0, 255)) + bytes((128, 128, 128, 255))
    )
    png = pngtools.encode_png(width, height, rows)
    chunks = _decode_png_chunks(png)
    assert chunks[b"IHDR"][0][:8] == struct.pack(">II", width, height)
    assert chunks[b"IEND"] == [b""]
    raw = zlib.decompress(b"".join(chunks[b"IDAT"]))
    stride = 1 + width * 3  # filter byte + RGB
    assert len(raw) == stride * height
    assert raw[0] == 0 and raw[stride] == 0  # filter None on both rows
    first = raw[1:stride]
    assert first == bytes((255, 0, 0)) + bytes((0, 255, 0)) + bytes((0, 0, 255))


def test_encode_png_rejects_mismatched_buffer() -> None:
    with pytest.raises(ValueError):
        pngtools.encode_png(2, 2, bytes(7))


def test_downscale_caps_dimensions_and_stays_deterministic() -> None:
    width, height = 64, 48
    buffer = bytes((i * 13) % 256 for i in range(width * height * 4))
    out, out_w, out_h = pngtools.downscale_bgrx(buffer, width, height, 16)
    assert max(out_w, out_h) <= 16
    assert len(out) == out_w * out_h * 4
    again, w2, h2 = pngtools.downscale_bgrx(buffer, width, height, 16)
    assert (out, out_w, out_h) == (again, w2, h2)
    untouched, uw, uh = pngtools.downscale_bgrx(buffer, width, height, 1000)
    assert (uw, uh) == (width, height) and untouched == buffer


def test_frame_quality_separates_blank_from_meaningful() -> None:
    blank = bytes(200 * 100 * 4)
    verdict = pngtools.frame_quality(blank, 200, 100)
    assert verdict["meaningful"] is False
    noise = bytes(((i // 4) * 37 + (i % 4) * 91) % 256 for i in range(200 * 100 * 4))
    verdict = pngtools.frame_quality(noise, 200, 100)
    assert verdict["meaningful"] is True
    assert verdict["unique_colors"] > 4
    assert max(verdict["channel_ranges"]) >= 8


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def test_redact_text_never_echoes_content() -> None:
    secret = "hunter2-bearer-token"
    record = redact.redact_text(secret)
    dumped = json.dumps(record)
    assert secret not in dumped
    assert record["chars"] == len(secret)
    assert len(record["sha256_16"]) == 16
    assert redact.redact_text(secret) == record  # deterministic


def test_scrub_masks_secret_shaped_values_but_keeps_safe_text() -> None:
    text = "signed in with password=hunter2 and token: abc123 for user ana"
    scrubbed = redact.scrub(text, limit=500)
    assert "hunter2" not in scrubbed
    assert "abc123" not in scrubbed
    assert "ana" in scrubbed


def test_scrub_clips_to_limit() -> None:
    assert len(redact.scrub("x" * 5000, limit=100)) <= 100 + len("[redacted:clipped]")


# --------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------


def test_action_digest_is_payload_canonical() -> None:
    a = action_digest("desktop.open_terminal", {"shell": "cmd", "keep_open": True})
    b = action_digest("desktop.open_terminal", {"keep_open": True, "shell": "cmd"})
    c = action_digest("desktop.open_terminal", {"shell": "wt", "keep_open": True})
    assert a == b
    assert a != c
    # the token itself never participates in the digest
    d = action_digest(
        "desktop.open_terminal",
        {"shell": "cmd", "keep_open": True, "confirmation_token": "whatever"},
    )
    assert d == a


def test_consequential_classification() -> None:
    assert is_consequential("desktop.open_terminal", {})
    assert is_consequential("desktop.launch_app", {"shortcut_path": "x.lnk"})
    assert is_consequential("desktop.show_window", {"mode": "restore_visible"})
    assert not is_consequential("desktop.show_window", {"mode": "restore_noactivate"})
    assert not is_consequential("desktop.capture", {})
    assert not is_consequential("desktop.type_text", {"text": "hi"})
    assert CONSEQUENTIAL_ACTIONS >= {"desktop.open_terminal", "desktop.launch_app"}


def test_approval_store_issue_redeem_is_single_use(tmp_path: Path) -> None:
    clock = [1000.0]
    store = ApprovalStore(tmp_path / "approvals.json", now=lambda: clock[0], ttl_seconds=60.0)
    digest = action_digest("desktop.open_terminal", {"shell": "cmd"})
    token = store.issue(digest, session_id="unit")
    raw = (tmp_path / "approvals.json").read_text(encoding="utf-8")
    assert token not in raw  # only the hash is durable

    store2 = ApprovalStore(tmp_path / "approvals.json", now=lambda: clock[0], ttl_seconds=60.0)
    store2.redeem(token, digest, "unit")  # fresh process redeems
    with pytest.raises(ApprovalRejected) as replay:
        store2.redeem(token, digest, "unit")
    assert replay.value.reason == "approval_already_used"


def test_approval_store_rejects_wrong_digest_session_and_expiry(tmp_path: Path) -> None:
    clock = [1000.0]
    store = ApprovalStore(tmp_path / "approvals.json", now=lambda: clock[0], ttl_seconds=30.0)
    digest = action_digest("desktop.launch_app", {"shortcut_path": "a.lnk"})
    token = store.issue(digest, session_id="unit")

    with pytest.raises(ApprovalRejected) as wrong_action:
        store.redeem(token, action_digest("desktop.launch_app", {"shortcut_path": "b.lnk"}), "unit")
    assert wrong_action.value.reason == "approval_wrong_action"

    with pytest.raises(ApprovalRejected) as wrong_session:
        store.redeem(token, digest, "other")
    assert wrong_session.value.reason == "approval_wrong_session"

    clock[0] += 31.0
    with pytest.raises(ApprovalRejected) as expired:
        store.redeem(token, digest, "unit")
    assert expired.value.reason == "approval_expired"


def test_approval_store_unknown_and_tampered_inputs_fail_closed(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.json", ttl_seconds=60.0)
    digest = action_digest("desktop.open_terminal", {})
    with pytest.raises(ApprovalRejected) as unknown:
        store.redeem("forged-token-value", digest, "unit")
    assert unknown.value.reason == "approval_unknown"
    with pytest.raises(ApprovalRejected) as missing:
        store.redeem(None, digest, "unit")
    assert missing.value.reason == "approval_missing"

    (tmp_path / "approvals.json").write_text("{not json", encoding="utf-8")
    broken = ApprovalStore(tmp_path / "approvals.json", ttl_seconds=60.0)
    with pytest.raises(ApprovalRejected) as tampered:
        broken.redeem("anything", digest, "unit")
    assert tampered.value.reason == "approval_unknown"


# --------------------------------------------------------------------------
# keymap
# --------------------------------------------------------------------------


def test_parse_key_named_and_character_keys() -> None:
    assert parse_key("enter") == ([], 0x0D)
    assert parse_key("a") == ([], ord("A"))
    assert parse_key("f5") == ([], 0x74)
    mods, vk = parse_key("ctrl+shift+s")
    assert vk == ord("S")
    assert mods == [0x11, 0x10]  # ctrl down before shift down


def test_parse_key_rejects_global_modifier_combos() -> None:
    for bad in ("win+l", "super+d", "meta+x", "ctrl+win+f"):
        with pytest.raises(InvalidActionError):
            parse_key(bad)


def test_parse_key_rejects_unknown_names() -> None:
    for bad in ("", "nope", "ctrl+", "ctrl++"):
        with pytest.raises(InvalidActionError):
            parse_key(bad)


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def test_window_identity_roundtrip_and_public_shape() -> None:
    ident = WindowIdentity(
        hwnd=17,
        pid=99,
        title="Some Window",
        class_name="App",
        rect=(0, 0, 800, 600),
        dpi=144,
        minimized=False,
        process_create_time=123.456,
        process_executable_digest="ab" * 32,
    )
    data = ident.to_dict()
    assert WindowIdentity.from_dict(data) == ident
    public = ident.public()
    assert public["window_id"] == 17
    assert public["width"] == 800 and public["height"] == 600
    assert public["dpi_scale"] == pytest.approx(1.5)
    assert "process_create_time" not in public


# --------------------------------------------------------------------------
# session: attach / binding / identity pinning
# --------------------------------------------------------------------------


def test_attach_pins_hwnd_pid_and_process_identity(tmp_path: Path) -> None:
    probe = FakeProbe()
    sample_window(probe)
    session = DesktopSession(tmp_path / "runtime" / "cu", "unit", probe)
    binding = session.attach("editor", title_contains="Test Editor", access="control")
    assert binding.identity.hwnd == 1001
    assert binding.identity.pid == 4242
    assert binding.identity.process_create_time == pytest.approx(1_700_000_000.0 + 4242)
    # durable across reload
    session2 = DesktopSession(tmp_path / "runtime" / "cu", "unit", probe)
    bound = session2.bound("editor")
    assert bound.identity.hwnd == 1001
    assert bound.access == "control"


def test_attach_validation_is_fail_closed(tmp_path: Path) -> None:
    probe = FakeProbe()
    sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    with pytest.raises(InvalidActionError):
        session.attach("Bad ID!", title_contains="Test")
    with pytest.raises(InvalidActionError):
        session.attach("editor", access="root")
    with pytest.raises(InvalidActionError):
        session.attach("editor")  # no selector
    with pytest.raises(TargetLostError):
        session.attach("editor", title_contains="Nothing Matches This")


def test_bound_detects_hwnd_reuse_by_another_process(tmp_path: Path) -> None:
    probe = FakeProbe()
    sample_window(probe, hwnd=55, pid=100)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    session.attach("victim", window_id=55, access="control")

    # The window disappears and the HWND is reused by a different PID.
    probe.windows[55].pid = 777
    probe.processes[777] = (9_999.0, "C:\\evil.exe")
    with pytest.raises(TargetLostError):
        session.bound("victim")


def test_bound_detects_process_restart_under_same_pid(tmp_path: Path) -> None:
    probe = FakeProbe()
    sample_window(probe, hwnd=55, pid=100)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    session.attach("victim", window_id=55, access="control")

    probe.processes[100] = (5_555.0, "C:\\app100.exe")  # restarted: new create time
    with pytest.raises(TargetLostError):
        session.bound("victim")


def test_control_gate_rejects_observe_only_binding(tmp_path: Path) -> None:
    probe = FakeProbe()
    sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    session.attach("editor", title_contains="Test Editor", access="observe")
    with pytest.raises(DesktopSecurityError):
        session.bound("editor", control=True)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------


def _valid_png(png: bytes) -> bool:
    try:
        _decode_png_chunks(png)
        return True
    except AssertionError:
        return False


def test_capture_returns_valid_png_with_diagnostics(tmp_path: Path) -> None:
    from karox.computer_use.capture import capture_window

    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="observe")
    result = capture_window(probe, binding.identity, deadline=Deadline.after(5.0))
    assert result.method == "win32-printwindow"
    assert _valid_png(result.png)
    assert (result.width, result.height) == (800, 600)
    assert result.quality["meaningful"] is True
    assert len(result.sha256) == 64


def test_capture_falls_back_from_blank_frames(tmp_path: Path) -> None:
    from karox.computer_use.capture import capture_window

    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="observe")

    real_bits = probe.capture_bits

    def blank_printwindow(hwnd: int, width: int, height: int, backend: str) -> bytes | None:
        if backend == "printwindow":
            return bytes(width * height * 4)
        return real_bits(hwnd, width, height, backend)

    probe.capture_bits = blank_printwindow  # type: ignore[method-assign]
    result = capture_window(probe, binding.identity, deadline=Deadline.after(5.0))
    assert result.method == "win32-wm-print"
    assert result.quality["meaningful"] is True
    assert any(item.get("backend") == "win32-printwindow" for item in result.fallbacks)


def test_capture_rejects_total_failure(tmp_path: Path) -> None:
    from karox.computer_use.capture import CaptureError, capture_window

    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="observe")
    probe.dib_mode = "fail"
    with pytest.raises(CaptureError):
        capture_window(probe, binding.identity, deadline=Deadline.after(5.0))


def test_capture_scales_down_large_frames(tmp_path: Path) -> None:
    from karox.computer_use.capture import capture_window

    probe = FakeProbe()
    big = FakeWindow(777, "Big Window", "App", (0, 0, 3840, 2160), 31)
    probe.add_window(big)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("big", window_id=777, access="observe")
    result = capture_window(probe, binding.identity, deadline=Deadline.after(10.0), max_dim=1600)
    assert max(result.width, result.height) <= 1600
    assert _valid_png(result.png)


# --------------------------------------------------------------------------
# inspection
# --------------------------------------------------------------------------


def test_inspection_returns_control_tree_and_redacted_evidence(tmp_path: Path) -> None:
    from karox.computer_use.inspection import inspect_window

    probe = FakeProbe()
    window = sample_window(probe)
    edit = FakeWindow(
        probe.new_hwnd(), "", "Edit", (110, 160, 890, 690), window.pid,
        text="the document body", styles=WS_VISIBLE,
    )
    disabled = FakeWindow(
        probe.new_hwnd(), "", "Button", (110, 110, 200, 150), window.pid,
        text="Save", styles=WS_VISIBLE | WS_DISABLED,
    )
    probe.add_child(window.hwnd, edit)
    probe.add_child(window.hwnd, disabled)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="observe")

    tree = inspect_window(probe, binding.identity, deadline=Deadline.after(5.0))
    classes = {node["class"] for node in tree["nodes"]}
    assert {"Edit", "Button"} <= classes
    by_class = {node["class"]: node for node in tree["nodes"]}
    assert by_class["Button"]["enabled"] is False
    assert by_class["Edit"]["enabled"] is True
    assert by_class["Edit"]["text"] == "the document body"
    assert tree["health"]["is_foreground"] is False

    evidence = tree["evidence"]
    dumped = json.dumps(evidence)
    assert "the document body" not in dumped
    assert evidence["node_count"] == 2


def test_inspection_caps_node_text(tmp_path: Path) -> None:
    from karox.computer_use.inspection import inspect_window

    probe = FakeProbe()
    window = sample_window(probe)
    edit = FakeWindow(
        probe.new_hwnd(), "", "Edit", (110, 160, 890, 690), window.pid, text="x" * 5000,
    )
    probe.add_child(window.hwnd, edit)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="observe")
    tree = inspect_window(probe, binding.identity, deadline=Deadline.after(5.0))
    assert len(tree["nodes"][0]["text"]) <= 512


# --------------------------------------------------------------------------
# background input
# --------------------------------------------------------------------------


def test_type_text_posts_wm_char_units_to_focus_target(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    edit = FakeWindow(probe.new_hwnd(), "", "Edit", (110, 160, 890, 690), window.pid)
    probe.add_child(window.hwnd, edit)
    probe.focus_targets[window.hwnd] = (4242, window.pid, edit.hwnd, edit.hwnd, window.hwnd)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")

    input_module.type_text(probe, binding.identity, "ab", deadline=Deadline.after(5.0))
    assert [(msg, wParam) for (_, msg, wParam, _) in probe.posted if msg == WM_CHAR] == [
        (WM_CHAR, ord("a")),
        (WM_CHAR, ord("b")),
    ]
    # posted to the child that owns keyboard focus, not the top-level window
    assert {hwnd for (hwnd, msg, _, _) in probe.posted if msg == WM_CHAR} == {edit.hwnd}


def test_type_text_rejects_oversize_and_nul(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    with pytest.raises(InvalidActionError):
        input_module.type_text(probe, binding.identity, "", deadline=Deadline.after(1.0))
    with pytest.raises(InvalidActionError):
        input_module.type_text(probe, binding.identity, "a\x00b", deadline=Deadline.after(1.0))
    with pytest.raises(InvalidActionError):
        input_module.type_text(
            probe, binding.identity, "x" * 40_001, deadline=Deadline.after(1.0)
        )


def test_press_key_posts_modifier_order(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    input_module.press_key(probe, binding.identity, "ctrl+s", deadline=Deadline.after(5.0))
    from _computer_use_fake import WM_KEYDOWN, WM_KEYUP

    sequence = [(msg, wParam) for (_, msg, wParam, _) in probe.posted]
    assert sequence == [
        (WM_KEYDOWN, 0x11),
        (WM_KEYDOWN, ord("S")),
        (WM_KEYUP, ord("S")),
        (WM_KEYUP, 0x11),
    ]


def test_click_targets_deepest_child_with_client_coordinates(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)  # rect (100,100)-(900,700)
    child = FakeWindow(probe.new_hwnd(), "", "Edit", (150, 150, 850, 650), window.pid)
    probe.add_child(window.hwnd, child)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")

    input_module.click(probe, binding.identity, 200, 200, deadline=Deadline.after(5.0))
    messages = [(hwnd, msg, wParam, lParam) for (hwnd, msg, wParam, lParam) in probe.posted]
    assert [item[1] for item in messages] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]
    assert {item[0] for item in messages} == {child.hwnd}
    # window point (200,200) is screen (300,300); the child origin is (150,150),
    # so the posted client coordinates are (150,150)
    packed = (150 & 0xFFFF) | ((150 & 0xFFFF) << 16)
    assert messages[1][3] == packed


def test_click_outside_window_is_refused(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    with pytest.raises(DesktopSecurityError):
        input_module.click(probe, binding.identity, 5000, 5, deadline=Deadline.after(1.0))
    with pytest.raises(DesktopSecurityError):
        input_module.click(probe, binding.identity, -1, 5, deadline=Deadline.after(1.0))
    assert probe.posted == []


def test_click_respects_button_and_deadline(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    input_module.click(
        probe, binding.identity, 10, 10, button="right", deadline=Deadline.after(5.0)
    )
    assert [msg for (_, msg, _, _) in probe.posted] == [
        WM_MOUSEMOVE, WM_RBUTTONDOWN, 0x0205,
    ]
    probe.posted.clear()
    expired = Deadline(lambda: 100.0, 99.0)
    with pytest.raises(DesktopTimeoutError):
        input_module.click(probe, binding.identity, 10, 10, deadline=expired)
    assert probe.posted == []


def test_scroll_posts_wheel_with_screen_coordinates(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)  # origin (100,100)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    input_module.scroll(
        probe, binding.identity, x=50, y=60, clicks=-3, deadline=Deadline.after(5.0)
    )
    assert len(probe.posted) == 3
    for hwnd, msg, wParam, lParam in probe.posted:
        assert hwnd == window.hwnd
        assert msg == WM_MOUSEWHEEL
        delta = wParam >> 16
        assert delta == -120  # signed 16-bit
        # wheel lParam carries SCREEN coordinates of the pointer position
        assert (lParam & 0xFFFF) == 100 + 50
        assert ((lParam >> 16) & 0xFFFF) == 100 + 60


def test_input_post_failure_is_typed(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    probe.post_failures.add(WM_CHAR)
    with pytest.raises(ComputerUseError):
        input_module.type_text(probe, binding.identity, "hi", deadline=Deadline.after(5.0))


# --------------------------------------------------------------------------
# launch / visible windows
# --------------------------------------------------------------------------


def test_launch_requires_start_menu_shortcut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = FakeProbe()
    programs = tmp_path / "start" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs.mkdir(parents=True)
    shortcut = programs / "KaroX Test App.lnk"
    shortcut.write_bytes(b"\x00")
    monkeypatch.setenv("APPDATA", str(tmp_path / "start"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))

    with pytest.raises(DesktopSecurityError):
        launch_module.launch_shortcut(
            probe, "C:\\Windows\\System32\\cmd.exe", deadline=Deadline.after(2.0)
        )
    with pytest.raises(DesktopSecurityError):
        launch_module.launch_shortcut(
            probe, str(programs / "evil.exe"), deadline=Deadline.after(2.0)
        )

    result = launch_module.launch_shortcut(
        probe, str(shortcut), deadline=Deadline.after(2.0)
    )
    assert result["launched"] is True
    target, arguments, cwd, show = probe.shell_executions[0]
    assert target == str(shortcut)
    assert show == 7  # SW_SHOWMINNOACTIVE: never activates the user's session
    assert result["activation_requested"] is False


def test_launch_visible_uses_shownormal_and_is_honest_about_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = FakeProbe()
    programs = tmp_path / "start" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs.mkdir(parents=True)
    shortcut = programs / "KaroX Test App.lnk"
    shortcut.write_bytes(b"\x00")
    monkeypatch.setenv("APPDATA", str(tmp_path / "start"))

    result = launch_module.launch_shortcut(
        probe, str(shortcut), visible=True, deadline=Deadline.after(2.0)
    )
    assert probe.shell_executions[0][3] == 1  # SW_SHOWNORMAL
    assert result["visible_requested"] is True
    assert result["activation_guaranteed"] is False


def test_open_terminal_launches_real_visible_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = FakeProbe()
    monkeypatch.setenv("ComSpec", "C:\\Windows\\System32\\cmd.exe")
    spawned_pid = 31337

    def on_launch(target: str, arguments: str, cwd: str, show: int) -> None:
        window = FakeWindow(
            probe.new_hwnd(), "Command Prompt - cmd", "ConsoleWindowClass",
            (50, 50, 850, 650), spawned_pid,
        )
        probe.add_window(window)
        probe.processes[spawned_pid] = (2_000.0, target)

    probe.on_shell_execute = on_launch
    probe.shell_result = spawned_pid

    result = launch_module.open_terminal(
        probe, shell="cmd", keep_open=True, deadline=Deadline.after(3.0)
    )
    assert result["shell"] == "cmd"
    assert result["window"] is not None
    assert result["window"]["process_id"] == spawned_pid
    assert result["visible"] is True
    target, arguments, _, show = probe.shell_executions[0]
    assert target.endswith("cmd.exe")
    assert arguments.startswith("/K")
    assert show == 1  # a requested visible window is shown, not hidden


def test_open_terminal_rejects_unknown_shell(tmp_path: Path) -> None:
    probe = FakeProbe()
    with pytest.raises(InvalidActionError):
        launch_module.open_terminal(probe, shell="rm -rf /", deadline=Deadline.after(1.0))


def test_open_terminal_reports_honestly_when_no_window_appears(tmp_path: Path) -> None:
    probe = FakeProbe()
    probe.shell_result = 4242
    result = launch_module.open_terminal(
        probe, shell="cmd", keep_open=True, deadline=Deadline.after(0.05)
    )
    assert result["window"] is None
    assert result["window_matched"] is False


def test_restore_without_activation(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    window.iconic = True
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    result = launch_module.restore_window(probe, binding.identity, mode="restore_noactivate")
    assert window.iconic is False
    assert result["activated"] is False
    assert result["restored"] is True


def test_show_window_visible_mode(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    window.iconic = True
    window.visible = False
    session = DesktopSession(tmp_path / "cu", "unit", probe)
    binding = session.attach("editor", window_id=window.hwnd, access="control")
    result = launch_module.restore_window(probe, binding.identity, mode="restore_visible")
    assert result["visible_requested"] is True
    assert result["activated"] is False  # never explicitly activates
    assert window.visible is True


# --------------------------------------------------------------------------
# adapter surface
# --------------------------------------------------------------------------


def test_adapter_action_table_kinds() -> None:
    assert ACTION_KINDS["desktop.list_windows"] == "observation"
    assert ACTION_KINDS["desktop.capture"] == "observation"
    assert ACTION_KINDS["desktop.inspect_ui"] == "observation"
    assert ACTION_KINDS["desktop.type_text"] == "action"
    assert ACTION_KINDS["desktop.click"] == "action"
    assert ACTION_KINDS["desktop.scroll"] == "action"
    assert ACTION_KINDS["desktop.launch_app"] == "action"
    assert ACTION_KINDS["desktop.open_terminal"] == "action"
    assert set(TOOL_SPECS) == set(ACTION_KINDS)
    for spec in TOOL_SPECS.values():
        assert spec["input_schema"]["type"] == "object"


def test_adapter_unknown_action_is_structured(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, FakeProbe())
    result = adapter.execute("desktop.self_destruct", {})
    assert isinstance(result, ToolResult)
    assert result.ok is False
    assert result.error is not None
    assert result.error["type"] == "invalid_action"


def test_adapter_status_reports_contract_without_touching_desktop(tmp_path: Path) -> None:
    probe = FakeProbe()
    adapter = make_adapter(tmp_path, probe)
    result = adapter.execute("desktop.status", {})
    assert result.ok is True
    assert result.kind == "observation"
    contract = result.data["control_contract"]
    assert contract["background_control"] is True
    assert contract["global_keyboard_injection"] is False
    assert contract["global_pointer_injection"] is False
    assert contract["focus_steal_blocked"] is True
    assert result.data["platform_available"] is True


def test_adapter_full_background_flow(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    edit = FakeWindow(probe.new_hwnd(), "", "Edit", (110, 160, 890, 690), window.pid)
    probe.add_child(window.hwnd, edit)
    adapter = make_adapter(tmp_path, probe)

    listed = adapter.execute("desktop.list_windows", {"title_contains": "Test Editor"})
    assert listed.ok and len(listed.data["windows"]) == 1

    attached = adapter.execute(
        "desktop.attach",
        {"app_id": "editor", "window_id": window.hwnd, "access": "control"},
    )
    assert attached.ok is True
    assert attached.data["window"]["window_id"] == window.hwnd

    typed = adapter.execute("desktop.type_text", {"app_id": "editor", "text": "secret phrase"})
    assert typed.ok is True
    evidence_dump = json.dumps(typed.evidence)
    assert "secret phrase" not in evidence_dump
    assert typed.evidence["redacted"]["text_chars"] == len("secret phrase")

    captured = adapter.execute("desktop.capture", {"app_id": "editor"})
    assert captured.ok is True
    artifact = captured.data["artifact"]
    artifact_path = Path(artifact["path"])
    assert artifact_path.is_file()
    assert _valid_png(artifact_path.read_bytes())
    assert "secret phrase" not in json.dumps(captured.evidence)

    inspected = adapter.execute("desktop.inspect_ui", {"app_id": "editor"})
    assert inspected.ok is True
    assert any(node["class"] == "Edit" for node in inspected.data["nodes"])
    # the evidence channel is redacted: control text stays in data, not evidence
    assert "the document body" not in json.dumps(inspected.evidence)
    assert inspected.evidence["redacted"]["node_text_total_chars"] >= 0


def test_adapter_input_without_attach_is_refused(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, FakeProbe())
    result = adapter.execute("desktop.type_text", {"app_id": "ghost", "text": "x"})
    assert result.ok is False
    assert result.error["type"] == "desktop_security"


def test_adapter_consequential_action_requires_one_shot_approval(tmp_path: Path) -> None:
    probe = FakeProbe()
    adapter = make_adapter(tmp_path, probe)
    payload = {"shell": "cmd", "keep_open": True}

    blocked = adapter.execute("desktop.open_terminal", payload)
    assert blocked.ok is False
    assert blocked.error["type"] == "approval_required"
    digest = blocked.error["action_digest"]
    assert blocked.error["instructions"]

    # forged / borrowed tokens fail closed
    forged = adapter.execute("desktop.open_terminal", payload, confirmation_token="nope")
    assert forged.ok is False
    assert forged.error["type"] == "approval_rejected"
    assert forged.error["reason"] == "approval_unknown"

    # a human layer issues the token out-of-band; exact action redeems once
    store = adapter.approvals
    token = store.issue(digest, session_id=adapter.session_id)
    approved = adapter.execute("desktop.open_terminal", payload, confirmation_token=token)
    assert approved.ok is True or approved.error["type"] != "approval_rejected"

    replay = adapter.execute("desktop.open_terminal", payload, confirmation_token=token)
    assert replay.ok is False
    assert replay.error["type"] == "approval_rejected"
    assert replay.error["reason"] == "approval_already_used"

    # the digest binds the exact payload: another shell needs a new approval
    other = adapter.execute(
        "desktop.open_terminal", {"shell": "wt", "keep_open": True}, confirmation_token=token
    )
    assert other.ok is False
    assert other.error["type"] in {"approval_rejected", "approval_required"}


def test_adapter_capture_digest_binding_for_approval(tmp_path: Path) -> None:
    """Approval digests must survive process restarts (durable store)."""
    probe = FakeProbe()
    adapter = make_adapter(tmp_path, probe, session="s1")
    blocked = adapter.execute("desktop.launch_app", {"shortcut_path": "C:\\x\\a.lnk"})
    digest = blocked.error["action_digest"]

    restarted = make_adapter(tmp_path, FakeProbe(), session="s1")
    token = restarted.approvals.issue(digest, session_id="s1")
    # redemption in yet another process
    third = make_adapter(tmp_path, FakeProbe(), session="s1")
    result = third.execute(
        "desktop.launch_app", {"shortcut_path": "C:\\x\\a.lnk"}, confirmation_token=token
    )
    # launch itself fails (shortcut does not exist) but NOT on approval grounds
    assert result.error is None or result.error["type"] != "approval_rejected"


def test_adapter_session_isolation(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    first = make_adapter(tmp_path, probe, session="alpha")
    first.execute("desktop.attach", {"app_id": "editor", "window_id": window.hwnd})
    second = make_adapter(tmp_path, probe, session="beta")
    result = second.execute("desktop.capture", {"app_id": "editor"})
    assert result.ok is False  # session beta never attached anything


def test_adapter_timeout_is_structured(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    adapter = make_adapter(tmp_path, probe)
    adapter.execute("desktop.attach", {"app_id": "editor", "window_id": window.hwnd, "access": "control"})
    result = adapter.execute(
        "desktop.type_text", {"app_id": "editor", "text": "hello"}, timeout=-1.0
    )
    assert result.ok is False
    assert result.error["type"] == "desktop_timeout"


def test_tool_result_serializes_to_one_json_line() -> None:
    result = ToolResult(
        action="desktop.status", kind="observation", ok=True, data={"a": 1}, evidence={}
    )
    line = result.to_json_line()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["action"] == "desktop.status"
    assert parsed["ok"] is True


def test_adapter_invalid_payload_is_structured(tmp_path: Path) -> None:
    probe = FakeProbe()
    window = sample_window(probe)
    adapter = make_adapter(tmp_path, probe)
    adapter.execute("desktop.attach", {"app_id": "editor", "window_id": window.hwnd, "access": "control"})
    result = adapter.execute("desktop.click", {"app_id": "editor", "x": "ten", "y": 0})
    assert result.ok is False
    assert result.error["type"] == "invalid_action"


# --------------------------------------------------------------------------
# desktop_apps facade (hosted app.* surface)
# --------------------------------------------------------------------------


def _hosted_runtime(profile: str) -> FakeRuntime:
    from karox.models import AccessProfile, Origin, OriginKind
    from karox.policy import CapabilityPolicy

    policy = CapabilityPolicy(profile=AccessProfile(profile))
    origin = Origin(OriginKind.HOSTED_CLIENT, "test-bridge")
    return FakeRuntime(policy, origin)


def _facade_with_probe(runtime: FakeRuntime, probe: FakeProbe, tmp_path: Path) -> None:
    from karox import desktop_apps

    adapter = ComputerUseAdapter(
        runtime_root=tmp_path / "runtime", session_id="hosted", probe=probe
    )
    runtime._desktop_apps = desktop_apps.DesktopAppsController(adapter)  # type: ignore[attr-defined]


def test_facade_refuses_without_runtime() -> None:
    from karox import desktop_apps
    from karox.core import InvalidCommand

    with pytest.raises(InvalidCommand):
        desktop_apps.execute_desktop_app_action(None, "app.discover", {}, 1.0)


def test_facade_requires_elevated_profile_and_confirmation(tmp_path: Path) -> None:
    from karox import desktop_apps
    from karox.policy import PolicyDenied

    probe = FakeProbe()
    sample_window(probe)
    runtime = _hosted_runtime("workspace_write")
    _facade_with_probe(runtime, probe, tmp_path)
    with pytest.raises(PolicyDenied):
        desktop_apps.execute_desktop_app_action(
            runtime, "app.discover", {"user_confirmed": True}, 5.0
        )


def test_facade_requires_user_confirmation_for_discovery(tmp_path: Path) -> None:
    from karox import desktop_apps
    from karox.computer_use.errors import DesktopSecurityError

    probe = FakeProbe()
    sample_window(probe)
    runtime = _hosted_runtime("elevated")
    _facade_with_probe(runtime, probe, tmp_path)
    with pytest.raises(DesktopSecurityError):
        desktop_apps.execute_desktop_app_action(runtime, "app.discover", {}, 5.0)


def test_facade_discover_attach_snapshot_flow(tmp_path: Path) -> None:
    from karox import desktop_apps

    probe = FakeProbe()
    window = sample_window(probe)
    runtime = _hosted_runtime("elevated")
    _facade_with_probe(runtime, probe, tmp_path)

    discovered = desktop_apps.execute_desktop_app_action(
        runtime, "app.discover", {"user_confirmed": True}, 5.0
    )
    assert any(item["window_id"] == window.hwnd for item in discovered["windows"])

    attached = desktop_apps.execute_desktop_app_action(
        runtime,
        "app.attach",
        {"user_confirmed": True, "app_id": "editor", "title_contains": "Test Editor",
         "access": "control"},
        5.0,
    )
    assert attached["attached"] is True
    assert attached["control_contract"]["background_control"] is True

    snapshot = desktop_apps.execute_desktop_app_action(
        runtime, "app.snapshot", {"app_id": "editor"}, 10.0
    )
    assert snapshot["capture_method"] == "win32-printwindow"
    assert snapshot["artifact"]["mime"] == "image/png"
    assert Path(snapshot["artifact"]["path"]).is_file()

    typed = desktop_apps.execute_desktop_app_action(
        runtime, "app.type", {"app_id": "editor", "text": "hello"}, 5.0
    )
    assert typed["typed"] is True
    assert "text" not in typed or typed.get("text") is None
    assert typed["text_length"] == 5

    detached = desktop_apps.execute_desktop_app_action(
        runtime, "app.detach", {"app_id": "editor"}, 5.0
    )
    assert detached["detached"] is True


def test_facade_rejects_foreground_capture_request(tmp_path: Path) -> None:
    from karox import desktop_apps
    from karox.computer_use.errors import DesktopSecurityError

    probe = FakeProbe()
    window = sample_window(probe)
    runtime = _hosted_runtime("elevated")
    _facade_with_probe(runtime, probe, tmp_path)
    desktop_apps.execute_desktop_app_action(
        runtime,
        "app.attach",
        {"user_confirmed": True, "app_id": "editor", "window_id": window.hwnd},
        5.0,
    )
    with pytest.raises(DesktopSecurityError):
        desktop_apps.execute_desktop_app_action(
            runtime, "app.snapshot", {"app_id": "editor", "bring_to_front": True}, 5.0
        )


def test_facade_unknown_action_is_invalid_command(tmp_path: Path) -> None:
    from karox import desktop_apps
    from karox.core import InvalidCommand

    runtime = _hosted_runtime("elevated")
    _facade_with_probe(runtime, FakeProbe(), tmp_path)
    with pytest.raises(InvalidCommand):
        desktop_apps.execute_desktop_app_action(
            runtime, "app.format_disk", {"user_confirmed": True}, 5.0
        )


def test_facade_respects_deadline(tmp_path: Path) -> None:
    from karox import desktop_apps
    from karox.computer_use.errors import DesktopTimeoutError

    probe = FakeProbe()
    window = sample_window(probe)
    runtime = _hosted_runtime("elevated")
    _facade_with_probe(runtime, probe, tmp_path)
    desktop_apps.execute_desktop_app_action(
        runtime,
        "app.attach",
        {"user_confirmed": True, "app_id": "editor", "window_id": window.hwnd,
         "access": "control"},
        5.0,
    )
    with pytest.raises(DesktopTimeoutError):
        desktop_apps.execute_desktop_app_action(
            runtime, "app.type", {"app_id": "editor", "text": "hello"}, -1.0
        )


# --------------------------------------------------------------------------
# CLI (python -m karox.computer_use) — the karo-core reachable path
# --------------------------------------------------------------------------


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "KAROX_RUNTIME_DIR": str(tmp_path / "runtime"),
        "KAROX_CONFIG_DIR": str(tmp_path / "config"),
        "PYTHONPATH": str(SRC_ROOT),
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "C:\\Windows"),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    return subprocess.run(
        [sys.executable, "-m", "karox.computer_use", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=120,
    )


def test_cli_status_emits_one_json_document(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path, "status", "--json")
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout.strip())
    assert document["ok"] is True
    assert document["action"] == "desktop.status"
    assert document["data"]["control_contract"]["global_pointer_injection"] is False


def test_cli_unknown_action_exit_code(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path, "destroy-everything", "--json")
    assert completed.returncode == 2
    document = json.loads(completed.stdout.strip())
    assert document["ok"] is False
    assert document["error"]["type"] == "invalid_action"


def test_cli_lists_actions_with_schemas(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path, "actions")
    assert completed.returncode == 0
    listing = json.loads(completed.stdout)
    names = {item["name"] for item in listing["tools"]}
    assert {"desktop.capture", "desktop.open_terminal", "desktop.list_windows"} <= names


def test_cli_consequential_needs_token_and_approve_refuses_non_tty(tmp_path: Path) -> None:
    blocked = _run_cli(tmp_path, "open_terminal", "--shell", "cmd", "--json")
    assert blocked.returncode == 3
    document = json.loads(blocked.stdout.strip())
    assert document["error"]["type"] == "approval_required"
    assert document["error"]["action_digest"]

    approve = _run_cli(
        tmp_path, "approve",
        "--action", "desktop.open_terminal",
        "--payload", json.dumps({"shell": "cmd", "keep_open": True}),
    )
    # no TTY in a captured subprocess: the human-only approve path must refuse
    assert approve.returncode == 3
    assert "tty" in (approve.stdout + approve.stderr).lower()


def test_cli_bad_payload_json_exits_2(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path, "attach", "--payload", "{not json", "--json")
    assert completed.returncode == 2
