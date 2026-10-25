"""Deterministic in-memory probe for computer-use unit tests.

The real probe talks to user32/gdi32 through ctypes. Every logic layer
(attachment, identity pinning, capture fallbacks, input targeting, approval
gating) is written against the probe protocol instead, so the contracts are
testable on any platform without touching the user's desktop. Live coverage of
the ctypes probe itself lives in ``test_computer_use_live.py``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

WS_VISIBLE = 0x10000000
WS_DISABLED = 0x08000000

WM_NULL = 0x0000
WM_GETTEXTLENGTH = 0x000E
WM_GETTEXT = 0x000D
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E


class FakeWindow:
    def __init__(
        self,
        hwnd: int,
        title: str,
        cls: str,
        rect: tuple[int, int, int, int],
        pid: int,
        *,
        visible: bool = True,
        iconic: bool = False,
        text: str = "",
        styles: int = WS_VISIBLE,
        dpi: int = 96,
    ) -> None:
        self.hwnd = hwnd
        self.title = title
        self.cls = cls
        self.rect = rect
        self.pid = pid
        self.visible = visible
        self.iconic = iconic
        self.text = text
        self.styles = styles
        self.dpi = dpi


class FakeProbe:
    """Satisfies the computer-use probe protocol with in-memory state."""

    available = True
    platform_name = "fake"

    def __init__(self) -> None:
        self.windows: dict[int, FakeWindow] = {}
        self.children: dict[int, list[FakeWindow]] = {}
        self.processes: dict[int, tuple[float, str]] = {}
        self.focus_targets: dict[int, tuple[int, int, int, int, int]] = {}
        self.posted: list[tuple[int, int, int, int]] = []
        self.sent: list[tuple[int, int, int, int, int]] = []
        self.post_failures: set[int] = set()
        self.sleeps: list[int] = []
        self.shell_executions: list[tuple[str, str, str, int]] = []
        self.shell_result: int = 33
        self.on_shell_execute: Optional[Callable[[str, str, str, int], None]] = None
        self.dib_mode: str = "gradient"
        self.dib_calls: list[tuple[int, int, int, str]] = []
        self.foreground: int = 0
        self.dpi_awareness: str = "per-monitor-v2"
        self.hung: set[int] = set()
        self._next_hwnd = 9000

    # --- window construction helpers -----------------------------------
    def add_window(self, window: FakeWindow) -> FakeWindow:
        self.windows[window.hwnd] = window
        self.processes.setdefault(window.pid, (1_700_000_000.0 + window.pid, f"C:\\app{window.pid}.exe"))
        return window

    def add_child(self, parent: int, window: FakeWindow) -> FakeWindow:
        self.children.setdefault(parent, []).append(window)
        self.windows.setdefault(window.hwnd, window)
        self.processes.setdefault(window.pid, self.processes.get(parent, (1.0, "x.exe")))
        return window

    def new_hwnd(self) -> int:
        self._next_hwnd += 1
        return self._next_hwnd

    # --- probe protocol -------------------------------------------------
    def ensure_dpi_awareness(self) -> str:
        return self.dpi_awareness

    def enum_windows(self) -> list[Any]:
        from karox.computer_use.win32 import RawWindow

        return [
            RawWindow(w.hwnd, w.title, w.rect, w.pid, w.visible, w.iconic)
            for w in self.windows.values()
            if w.hwnd not in {c.hwnd for kids in self.children.values() for c in kids}
        ]

    def enum_child_windows(self, hwnd: int) -> list[Any]:
        from karox.computer_use.win32 import RawWindow

        return [
            RawWindow(w.hwnd, w.title or w.cls, w.rect, w.pid, bool(w.styles & WS_VISIBLE), False)
            for w in self.children.get(hwnd, [])
        ]

    def window_text(self, hwnd: int, max_chars: int) -> str:
        window = self.windows.get(hwnd)
        return window.text[:max_chars] if window else ""

    def class_name(self, hwnd: int) -> str:
        window = self.windows.get(hwnd)
        return window.cls if window else ""

    def window_rect(self, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        window = self.windows.get(hwnd)
        return window.rect if window else None

    def client_rect(self, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        window = self.windows.get(hwnd)
        if window is None:
            return None
        return (0, 0, window.rect[2] - window.rect[0], window.rect[3] - window.rect[1])

    def screen_to_client(self, hwnd: int, x: int, y: int) -> Optional[tuple[int, int]]:
        window = self.windows.get(hwnd)
        if window is None:
            return None
        return (x - window.rect[0], y - window.rect[1])

    def child_from_point(self, hwnd: int, x: int, y: int) -> int:
        # The protocol passes CLIENT coordinates of the parent; child rects
        # are stored in screen space, so translate before hit-testing.
        parent = self.windows.get(hwnd)
        origin = (parent.rect[0], parent.rect[1]) if parent else (0, 0)
        screen_x, screen_y = origin[0] + x, origin[1] + y
        for child in self.children.get(hwnd, []):
            if not child.styles & WS_VISIBLE or child.styles & WS_DISABLED:
                continue
            left, top, right, bottom = child.rect
            if left <= screen_x < right and top <= screen_y < bottom:
                return child.hwnd
        return 0

    def window_pid(self, hwnd: int) -> int:
        window = self.windows.get(hwnd)
        return window.pid if window else 0

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def is_iconic(self, hwnd: int) -> bool:
        window = self.windows.get(hwnd)
        return bool(window and window.iconic)

    def is_visible(self, hwnd: int) -> bool:
        window = self.windows.get(hwnd)
        return bool(window and window.visible)

    def is_hung(self, hwnd: int) -> bool:
        return hwnd in self.hung

    def foreground_hwnd(self) -> int:
        return self.foreground

    def dpi_for_window(self, hwnd: int) -> int:
        window = self.windows.get(hwnd)
        return window.dpi if window else 0

    def root_ancestor(self, hwnd: int) -> int:
        for parent, kids in self.children.items():
            if any(child.hwnd == hwnd for child in kids):
                return parent
        return hwnd

    def window_styles(self, hwnd: int) -> int:
        window = self.windows.get(hwnd)
        return window.styles if window else 0

    def thread_focus(self, hwnd: int) -> tuple[int, int, int, int, int]:
        if hwnd in self.focus_targets:
            return self.focus_targets[hwnd]
        window = self.windows.get(hwnd)
        pid = window.pid if window else 0
        return (4242, pid, hwnd, 0, hwnd)

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
        if msg in self.post_failures:
            return False
        self.posted.append((hwnd, msg, wparam, lparam))
        return True

    def send_message_timeout(
        self, hwnd: int, msg: int, wparam: int, lparam: int, timeout_ms: int
    ) -> tuple[bool, int]:
        self.sent.append((hwnd, msg, wparam, lparam, timeout_ms))
        window = self.windows.get(hwnd)
        if window is None:
            return (False, 0)
        if msg == WM_GETTEXTLENGTH:
            return (True, len(window.text))
        if msg == WM_GETTEXT:
            return (True, 0)
        if msg == WM_NULL:
            return (hwnd not in self.hung, 0)
        return (True, 0)

    def get_text(self, hwnd: int, max_chars: int) -> str:
        window = self.windows.get(hwnd)
        return window.text[:max_chars] if window else ""

    def capture_bits(self, hwnd: int, width: int, height: int, backend: str) -> Optional[bytes]:
        self.dib_calls.append((hwnd, width, height, backend))
        if self.dib_mode == "fail":
            return None
        size = max(1, width) * max(1, height) * 4
        if self.dib_mode == "blank":
            return bytes(size)
        return bytes(((i // 4) * 7 + (i % 4) * 31) % 251 for i in range(size))

    def shell_execute(self, target: str, arguments: str, cwd: str, show_command: int) -> int:
        self.shell_executions.append((target, arguments, cwd, show_command))
        if self.on_shell_execute is not None:
            self.on_shell_execute(target, arguments, cwd, show_command)
        return self.shell_result

    def create_process(self, executable: str, arguments: str, cwd: Any, show: int) -> int:
        self.shell_executions.append((executable, arguments, str(cwd or ""), show))
        if self.on_shell_execute is not None:
            self.on_shell_execute(executable, arguments, str(cwd or ""), show)
        return self.shell_result

    def map_virtual_key(self, vk: int) -> int:
        return vk & 0xFF

    def show_window_async(self, hwnd: int, command: int) -> bool:
        window = self.windows.get(hwnd)
        if window is None:
            return False
        if command == 4:  # SW_SHOWNOACTIVATE
            window.iconic = False
        elif command == 7:  # SW_SHOWMINNOACTIVE
            window.iconic = True
        elif command in (1, 5):  # SW_SHOWNORMAL / SW_SHOW
            window.iconic = False
            window.visible = True
        return True

    def sleep_ms(self, ms: int) -> None:
        self.sleeps.append(ms)

    def process_info(self, pid: int) -> Optional[tuple[float, str]]:
        return self.processes.get(pid)


class FakeRuntime:
    """Duck-typed hosted runtime for the ``desktop_apps`` facade tests."""

    def __init__(self, policy: Any, origin: Any) -> None:
        self.policy = policy
        self.hosted_origin = origin
