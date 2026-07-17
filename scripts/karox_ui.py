"""KaroX console UI kit (v4.1) — one look & feel for every CLI surface.

Plain-text safe: colors are enabled only for interactive terminals and are
disabled by NO_COLOR / KAROX_NO_COLOR=1. Import must never fail loudly —
callers are expected to fall back to plain prints if this module is missing.
"""
from __future__ import annotations

import os
import sys

WIDTH = 64

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("KAROX_NO_COLOR") == "1":
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
                return False
            if not (mode.value & 0x0004):
                if kernel32.SetConsoleMode(handle, mode.value | 0x0004) == 0:
                    return False
        except Exception:
            return False
    return True


COLORS_ON = _colors_enabled()


def _c(code: str) -> str:
    return code if COLORS_ON else ""


RESET = _c("\x1b[0m")
BOLD = _c("\x1b[1m")
DIM = _c("\x1b[2m")
RED = _c("\x1b[31m")
GREEN = _c("\x1b[32m")
YELLOW = _c("\x1b[33m")
MAGENTA = _c("\x1b[35m")
CYAN = _c("\x1b[36m")
WHITE = _c("\x1b[37m")
GRAY = _c("\x1b[90m")


def banner(version: str = "", subtitle: str = "") -> None:
    version_text = f"  {DIM}{MAGENTA}v{version}{RESET}" if version else ""
    print("")
    print(f"  {YELLOW}★{RESET}  {BOLD}{WHITE}K A R O X{RESET}{version_text}")
    if subtitle:
        print(f"     {GRAY}{subtitle}{RESET}")
    print(f"  {MAGENTA}{'━' * (WIDTH - 2)}{RESET}")


def section(title: str) -> None:
    label = f" {str(title).upper()} "
    fill = max(2, WIDTH - len(label) - 5)
    print("")
    print(f"  {MAGENTA}┌─{RESET}{BOLD}{label}{RESET}{GRAY}{'─' * fill}{RESET}")


def kv(key: str, value: str) -> None:
    print(f"  {GRAY}│{RESET}  {GRAY}{str(key):<16}{RESET} {str(value)}")


def _mark(symbol: str, color: str, title: str, detail: str) -> None:
    tail = f"  {GRAY}{detail}{RESET}" if detail else ""
    print(f"  {color}{symbol}{RESET} {title}{tail}")


def ok(title: str, detail: str = "") -> None:
    _mark("✓", GREEN, title, detail)


def warn(title: str, detail: str = "") -> None:
    _mark("!", YELLOW, title, detail)


def fail(title: str, detail: str = "") -> None:
    _mark("×", RED, title, detail)


def info(title: str, detail: str = "") -> None:
    _mark("•", CYAN, title, detail)


def step(title: str, detail: str = "") -> None:
    _mark("◌", MAGENTA, title, detail)


def hr() -> None:
    print(f"  {GRAY}{'─' * (WIDTH - 2)}{RESET}")


def update_notice(version: str) -> None:
    text = f"↑ KaroX v{version} is available · run: karox update"
    inner = WIDTH - 6
    body = text[: inner - 2]
    print(f"  {MAGENTA}╭{'─' * inner}╮{RESET}")
    print(f"  {MAGENTA}│{RESET} {YELLOW}{body:<{inner - 2}}{RESET} {MAGENTA}│{RESET}")
    print(f"  {MAGENTA}╰{'─' * inner}╯{RESET}")
