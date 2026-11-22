"""Colours of the chat: one set of named roles, painted for the terminal at hand.

Every colour the UI uses is a role (``accent``, ``muted``, ``success`` ...), never a raw
code at the call site. A theme gives each role a colour; the terminal's colour depth
decides how it is written:

* 24-bit (Windows Terminal, VS Code, iTerm2, kitty, WezTerm, ``COLORTERM=truecolor``):
  the theme's own RGB colours;
* 256 colours (``TERM=*-256color``, the Windows console): the nearest xterm-256 colour;
* 16 colours (anything else, or ``theme = "ansi"``): the terminal's own ANSI palette,
  which follows whatever light or dark scheme the user has picked;
* none (``NO_COLOR``, ``--no-color``, a pipe): plain text.

Themes: ``dark`` and ``light`` (tuned, with tinted backgrounds for diffs), ``mid`` (the
default when the background is unknown: mid-tone colours readable on both, no tinted
backgrounds) and ``ansi`` (16 colours only). ``theme`` in config.toml or ``KAROX_THEME``
picks one; ``auto`` reads ``COLORFGBG`` when the terminal sets it.

Cheap to import (the start-up frame uses it before asyncio and prompt_toolkit).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

# role -> (16-colour SGR, mid, dark, light). Empty hex: no colour of its own in that
# theme (the 16-colour code still applies at 16 colours).
_ROLES: dict[str, tuple[str, str, str, str]] = {
    "accent": ("36", "5fafd7", "7aa2f7", "2563c9"),
    "brand": ("1;35", "af87ff", "bb9af7", "8250df"),
    "success": ("32", "5faf5f", "9ece6a", "2f7d32"),
    "error": ("31", "e05f5f", "f7768e", "c62828"),
    "warning": ("33", "d7af00", "e0af68", "9a6700"),
    "muted": ("2", "8a8a8a", "7c829c", "6e7781"),
    "subtle": ("2", "6c6c6c", "4a5070", "b0b7c3"),
    "code": ("36", "d7875f", "ff9e64", "b35900"),
    "heading": ("1;36", "5fafd7", "7aa2f7", "2563c9"),
    "user": ("1", "", "", ""),
    "keyword": ("35", "af87ff", "bb9af7", "8250df"),
    "string": ("32", "87af5f", "9ece6a", "2f7d32"),
    "number": ("33", "d7875f", "ff9e64", "b35900"),
    "comment": ("2", "808080", "5f6a8f", "8c959f"),
    "func": ("36", "5fafd7", "7dcfff", "0969da"),
}
_BOLD = {"brand", "heading", "user"}
_ITALIC = {"comment"}
# Tinted backgrounds of added / removed diff lines (dark and light themes only).
_BACKGROUNDS: dict[str, tuple[str, str]] = {
    "add_bg": ("1e3a2a", "dafbe1"),
    "del_bg": ("44212a", "ffebe9"),
}
# Names the chat used before roles existed.
ALIASES = {
    "dim": "muted",
    "red": "error",
    "green": "success",
    "yellow": "warning",
    "cyan": "accent",
}
THEMES = ("auto", "mid", "dark", "light", "ansi")
_ANSI_NAMES = {
    "30": "ansiblack",
    "31": "ansired",
    "32": "ansigreen",
    "33": "ansiyellow",
    "34": "ansiblue",
    "35": "ansimagenta",
    "36": "ansicyan",
    "37": "ansigray",
}


def color_depth(env: Mapping[str, str] | None = None, platform: str | None = None) -> int:
    """Colours the terminal shows: 0 (none), 4 (16), 8 (256) or 24 (true colour)."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if env.get("NO_COLOR"):
        return 0
    forced = env.get("KAROX_COLOR_DEPTH") or env.get("PROMPT_TOOLKIT_COLOR_DEPTH", "")
    for key, depth in (("24", 24), ("8", 8), ("4", 4), ("1", 0), ("0", 0)):
        if forced and forced.replace("DEPTH_", "").split("_")[0] == key:
            return depth
    term = env.get("TERM", "").lower()
    if term == "dumb":
        return 0
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 24
    if env.get("WT_SESSION") or env.get("TERM_PROGRAM", "") in (
        "vscode",
        "iTerm.app",
        "WezTerm",
        "ghostty",
        "Hyper",
    ):
        return 24
    if any(name in term for name in ("direct", "kitty", "alacritty", "wezterm", "ghostty")):
        return 24
    if "256" in term or platform == "win32":
        return 8
    return 4


def theme_name(setting: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """The theme to use: ``KAROX_THEME``, then the setting, then the terminal's hint."""
    env = os.environ if env is None else env
    name = (env.get("KAROX_THEME") or setting or "auto").strip().lower()
    if name not in THEMES:
        name = "auto"
    if name != "auto":
        return name
    fgbg = env.get("COLORFGBG", "")
    if fgbg:
        try:
            background = int(fgbg.split(";")[-1])
        except ValueError:
            return "mid"
        return "light" if background in (7, 15) or 9 <= background <= 14 else "dark"
    return "mid"


def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def xterm256(hex6: str) -> int:
    """The nearest colour of the xterm-256 palette (the 6x6x6 cube or the grey ramp)."""
    r, g, b = _rgb(hex6)
    steps = (0, 95, 135, 175, 215, 255)

    def near(v: int) -> int:
        return min(range(6), key=lambda i: abs(steps[i] - v))

    cr, cg, cb = near(r), near(g), near(b)
    cube = (steps[cr], steps[cg], steps[cb])
    grey_level = max(0, min(23, round((r + g + b) / 3 - 8) // 10))
    grey = 8 + grey_level * 10

    def dist(c: tuple[int, int, int]) -> int:
        return (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2

    if dist((grey, grey, grey)) < dist(cube):
        return 232 + grey_level
    return 16 + 36 * cr + 6 * cg + cb


def xterm_hex(index: int) -> str:
    """The RGB colour of an xterm-256 palette entry above 15, as "rrggbb"."""
    if index >= 232:
        level = 8 + (index - 232) * 10
        return f"{level:02x}{level:02x}{level:02x}"
    steps = (0, 95, 135, 175, 215, 255)
    index -= 16
    return f"{steps[index // 36]:02x}{steps[index // 6 % 6]:02x}{steps[index % 6]:02x}"


class Theme:
    """Roles painted for one theme and colour depth."""

    def __init__(self, name: str = "ansi", depth: int = 4) -> None:
        self.depth = depth
        self.name = "ansi" if depth <= 4 else name if name in THEMES[1:] else "mid"
        column = {"mid": 1, "dark": 2, "light": 3}.get(self.name, 0)
        self._sgr: dict[str, str] = {}
        self._pt: dict[str, str] = {}
        for role, row in _ROLES.items():
            hex6 = row[column] if column else ""
            flags = (["1"] if role in _BOLD else []) + (["3"] if role in _ITALIC else [])
            if depth == 0:
                self._sgr[role] = ""
                self._pt[role] = ""
            elif hex6:
                self._sgr[role] = ";".join([*flags, self._fg(hex6)])
                words = ["bold"] * (role in _BOLD) + ["italic"] * (role in _ITALIC)
                self._pt[role] = " ".join([*words, f"fg:#{self._shown(hex6)}"])
            else:
                self._sgr[role] = row[0]
                self._pt[role] = _pt_from_sgr(row[0])
        tinted = self.name in ("dark", "light") and depth >= 8
        for role, (dark, light) in _BACKGROUNDS.items():
            hex6 = dark if self.name == "dark" else light
            self._sgr[role] = self._bg(hex6) if tinted else ""
            self._pt[role] = f"bg:#{self._shown(hex6)}" if tinted else ""

    def _shown(self, hex6: str) -> str:
        """The colour the terminal will show: at 256 colours, the palette entry the SGR
        code names (so prompt_toolkit picks the very same entry)."""
        return hex6 if self.depth >= 24 else xterm_hex(xterm256(hex6))

    def _fg(self, hex6: str) -> str:
        if self.depth >= 24:
            return "38;2;{};{};{}".format(*_rgb(hex6))
        return f"38;5;{xterm256(hex6)}"

    def _bg(self, hex6: str) -> str:
        if self.depth >= 24:
            return "48;2;{};{};{}".format(*_rgb(hex6))
        return f"48;5;{xterm256(hex6)}"

    def sgr(self, role: str) -> str:
        """The SGR parameters of ``role`` ("" when it has no colour here)."""
        if role == "bold":
            return "1" if self.depth else ""
        if role == "italic":
            return "3" if self.depth else ""
        return self._sgr.get(ALIASES.get(role, role), "")

    def pt(self, role: str) -> str:
        """``role`` as a prompt_toolkit style string."""
        if role == "bold":
            return "bold"
        return self._pt.get(ALIASES.get(role, role), "")


def _pt_from_sgr(codes: str) -> str:
    words = []
    for code in codes.split(";"):
        if code == "1":
            words.append("bold")
        elif code == "2":
            words.append("dim")
        elif code == "3":
            words.append("italic")
        elif code in _ANSI_NAMES:
            words.append(_ANSI_NAMES[code])
    return " ".join(words)
