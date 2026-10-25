"""Application launch, visible-window actions and the real visible terminal.

Default launch is minimized and no-activate: the user's foreground session is
never disturbed. Visibility is a separate, explicit, approval-gated choice —
"show me the terminal" gets a real visible terminal window (Windows Terminal,
pwsh, powershell or conhost/cmd, first one available), launched through
``CreateProcessW`` so the process id, creation time and window binding are
provable. No arbitrary command strings are accepted here; general process
execution belongs to the guarded ``process.run`` surface, not to desktop tools.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

from .deadline import Deadline
from .errors import DesktopSecurityError, InvalidActionError, LaunchError
from .identity import WindowIdentity, capture_identity
from .win32 import SW_SHOWNORMAL, SW_SHOWNOACTIVATE, SW_SHOWMINNOACTIVE

TERMINAL_SHELLS = ("auto", "cmd", "powershell", "pwsh", "wt")
_MAX_SHORTCUTS = 300


def start_menu_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return tuple(roots)


def list_start_menu_apps(limit: int = _MAX_SHORTCUTS) -> dict[str, Any]:
    """Installed Start Menu shortcuts: the closed catalog app launch accepts."""

    apps: list[dict[str, str]] = []
    roots = start_menu_roots()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.lnk")):
            apps.append({"name": path.stem, "path": str(path)})
            if len(apps) >= max(1, int(limit)):
                return {"apps": apps, "roots": [str(item) for item in roots], "truncated": True}
    return {"apps": apps, "roots": [str(item) for item in roots], "truncated": False}


def validate_shortcut(raw: Any) -> Path:
    """Accept only an existing .lnk below a Windows Start Menu Programs root."""

    if not isinstance(raw, str) or not raw.strip():
        raise InvalidActionError("shortcut_path must be a non-empty string")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.suffix.casefold() != ".lnk":
        raise DesktopSecurityError("launch accepts only an absolute Start Menu .lnk shortcut")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LaunchError("the requested Start Menu shortcut does not exist") from exc
    for root in start_menu_roots():
        try:
            resolved.relative_to(root.resolve(strict=False))
            return resolved
        except ValueError:
            continue
    raise DesktopSecurityError("launch shortcut must stay inside Start Menu Programs")


def _wait_for_window(
    probe: Any,
    deadline: Deadline,
    *,
    pid: Optional[int] = None,
    title_contains: str = "",
) -> Optional[WindowIdentity]:
    """Bounded wait for a window to appear; honest None when it never does."""

    needle = title_contains.strip().casefold()
    first = True
    while True:
        deadline_ok = not deadline.expired
        if not (first or deadline_ok):
            return None
        first = False
        try:
            rows = probe.enum_windows()
        except Exception:
            rows = []
        for raw in rows:
            if pid is not None and int(raw.pid) == int(pid):
                try:
                    return capture_identity(probe, raw.hwnd)
                except Exception:
                    continue
            if needle and needle in (raw.title or "").casefold():
                try:
                    return capture_identity(probe, raw.hwnd)
                except Exception:
                    continue
        if deadline.expired:
            return None
        probe.sleep_ms(min(150, max(10, int(deadline.remaining() * 1000))))


def launch_shortcut(
    probe: Any,
    shortcut_path: Any,
    *,
    visible: bool = False,
    deadline: Optional[Deadline] = None,
    title_contains: str = "",
) -> dict[str, Any]:
    """Launch one installed Start Menu shortcut; minimized-noactivate by default.

    No executable arguments or shell text are accepted; Windows resolves the
    already-installed .lnk. Even a visible launch only *requests* normal
    display — the target application decides whether it self-activates, so the
    result reports a requested contract, never a focus guarantee.
    """

    deadline = deadline or Deadline.after(15.0)
    deadline.check("desktop launch")
    shortcut = validate_shortcut(shortcut_path)
    show = SW_SHOWNORMAL if visible else SW_SHOWMINNOACTIVE
    code = probe.shell_execute(str(shortcut), "", str(shortcut.parent), show)
    if code <= 32:
        raise LaunchError(f"Windows could not launch the Start Menu shortcut (code {code})")
    matched = None
    if title_contains:
        matched = _wait_for_window(probe, deadline, title_contains=title_contains)
    return {
        "launched": True,
        "shortcut_name": shortcut.name,
        "visible_requested": bool(visible),
        "activation_requested": False,
        "activation_guaranteed": False,
        "matched_window": matched.public() if matched is not None else None,
        "launch_contract": {
            "arguments_allowed": False,
            "start_menu_only": True,
            "show_command": "SW_SHOWNORMAL" if visible else "SW_SHOWMINNOACTIVE",
        },
    }


def resolve_terminal(shell: str = "auto") -> tuple[str, str, str]:
    """(shell name, executable, base arguments) for a real visible terminal."""

    if shell not in TERMINAL_SHELLS:
        raise InvalidActionError(
            f"unsupported shell {shell!r}; expected one of {', '.join(TERMINAL_SHELLS)}"
        )
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"

    def powershell_exe() -> Optional[str]:
        path = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return str(path) if path.is_file() else None

    candidates: list[tuple[str, Any]] = []
    if shell in ("auto", "wt"):
        def wt() -> Optional[str]:
            found = shutil.which("wt.exe")
            if found:
                return found
            alias = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe"
            return str(alias) if alias.is_file() else None

        candidates.append(("wt", wt))
    if shell in ("auto", "pwsh"):
        candidates.append(("pwsh", lambda: shutil.which("pwsh.exe")))
    if shell in ("auto", "powershell"):
        candidates.append(("powershell", powershell_exe))
    if shell in ("auto", "cmd"):
        def cmd() -> Optional[str]:
            comspec = os.environ.get("ComSpec")
            if comspec and Path(comspec).is_file():
                return comspec
            fallback = Path(system_root) / "System32" / "cmd.exe"
            return str(fallback) if fallback.is_file() else None

        candidates.append(("cmd", cmd))
    for name, resolver in candidates:
        executable = resolver()
        if executable:
            return name, executable, ""
    raise LaunchError(f"no {shell} terminal executable could be resolved on this system")


def _wait_for_new_window(
    probe: Any,
    before: set[int],
    deadline: Deadline,
) -> Optional[WindowIdentity]:
    """Wait for a visible top-level window that did not exist before launch.

    Windows 11 default-terminal delegation hosts new console sessions inside
    WindowsTerminal.exe, so the window's PID is not the launched process's PID.
    A window that appears exactly while the launch is in flight, with the
    launched process still alive, is reported as a ``new-window`` binding —
    real evidence, honestly weaker than a PID match.
    """

    first = True
    while True:
        if not (first or not deadline.expired):
            return None
        first = False
        try:
            rows = probe.enum_windows()
        except Exception:
            rows = []
        for raw in rows:
            if raw.hwnd in before or not raw.visible:
                continue
            try:
                return capture_identity(probe, raw.hwnd)
            except Exception:
                continue
        if deadline.expired:
            return None
        probe.sleep_ms(min(150, max(10, int(deadline.remaining() * 1000))))


def open_terminal(
    probe: Any,
    *,
    shell: str = "auto",
    keep_open: bool = True,
    deadline: Optional[Deadline] = None,
) -> dict[str, Any]:
    """Open a real, visible terminal window and prove which process owns it."""

    deadline = deadline or Deadline.after(15.0)
    deadline.check("desktop open_terminal")
    name, executable, _ = resolve_terminal(shell)
    if name == "cmd":
        arguments = "/K" if keep_open else ""
    elif name in ("powershell", "pwsh"):
        arguments = "-NoExit" if keep_open else ""
    else:  # wt manages its own session lifetime
        arguments = ""
    try:
        before = {raw.hwnd for raw in probe.enum_windows()}
    except Exception:
        before = set()
    pid = int(probe.create_process(executable, arguments, None, SW_SHOWNORMAL) or 0)
    if pid <= 0:
        raise LaunchError(f"Windows refused to create the {name} terminal process")
    matched = _wait_for_window(probe, deadline, pid=pid)
    binding = "process-match" if matched is not None else ""
    if matched is None:
        matched = _wait_for_new_window(probe, before, deadline)
        binding = "new-window" if matched is not None else ""
    if matched is None and name == "wt":
        # Last resort for the wt alias: a title match, weakest binding.
        matched = _wait_for_window(probe, Deadline.after(2.0), title_contains="terminal")
        binding = "title-match" if matched is not None else ""
    process_identity: dict[str, Any] = {"process_id": pid, "available": False}
    info = probe.process_info(pid)
    if info is not None:
        import hashlib

        create_time, exe = info
        process_identity = {
            "process_id": pid,
            "available": True,
            "create_time": create_time,
            "executable_sha256_16": hashlib.sha256(
                (exe or "").replace("\\", "/").casefold().encode("utf-8")
            ).hexdigest()[:16],
        }
    return {
        "shell": name,
        "visible": True,
        "keep_open": bool(keep_open),
        "process_id": pid,
        "window": matched.public() if matched is not None else None,
        "window_matched": matched is not None,
        "window_binding": binding or None,
        "evidence": {"process_identity": process_identity},
        "activation_note": (
            "a newly created visible window may take the foreground naturally; "
            "KaroX never activates windows explicitly"
        ),
    }


def restore_window(
    probe: Any,
    ident: WindowIdentity,
    *,
    mode: str = "restore_noactivate",
    deadline: Optional[Deadline] = None,
) -> dict[str, Any]:
    """Un-minimize one pinned window, with or without a visible restore."""

    deadline = deadline or Deadline.after(5.0)
    deadline.check("desktop show_window")
    if mode == "restore_noactivate":
        command = SW_SHOWNOACTIVATE
    elif mode == "restore_visible":
        command = SW_SHOWNORMAL
    else:
        raise InvalidActionError("mode must be restore_noactivate or restore_visible")
    if not probe.show_window_async(ident.hwnd, command):
        raise LaunchError("Windows refused to change the window state")
    restored = False
    for _ in range(20):
        if not probe.is_iconic(ident.hwnd):
            restored = True
            break
        if deadline.expired:
            break
        probe.sleep_ms(25)
    if not restored and probe.is_iconic(ident.hwnd):
        raise LaunchError("Windows did not restore the window")
    return {
        "restored": True,
        "mode": mode,
        "activated": False,
        "visible_requested": mode == "restore_visible",
        "window": ident.public(),
    }


__all__ = [
    "TERMINAL_SHELLS",
    "launch_shortcut",
    "list_start_menu_apps",
    "open_terminal",
    "resolve_terminal",
    "restore_window",
    "start_menu_roots",
    "validate_shortcut",
]
