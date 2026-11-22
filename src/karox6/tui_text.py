"""Text of the chat UI that needs no event loop and no terminal library: colours, glyphs,
the welcome card and the status bar.

It is shared by the chat (``tui``), the full-screen app (``tui_app``) and the start-up
frame (``splash``), which paints the first screen before asyncio and prompt_toolkit are
even imported; one source keeps that frame identical to the app's own first frame.
Imports here stay cheap on purpose (no asyncio, no prompt_toolkit at module level).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from karox6.tui_theme import Theme

_BOX_FANCY = ("╭", "╮", "╰", "╯", "─", "│")
_BOX_ASCII = ("+", "+", "+", "+", "-", "|")
_ANSI = re.compile("\x1b\\[[0-9;]*m")
_TERMINAL_CONTROL = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)
ELLIPSIS = "…"
DECO_START = "\ue001"  # private-use marks around decoration the full screen must not copy
DECO_END = "\ue002"


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Current chat metadata displayed by the full-screen status bar."""

    model: str
    provider: str
    workspace_name: str
    branch: str
    approval_mode: str
    running: bool
    activity: str
    total_tokens: int
    context_used: int
    context_window: int
    context_pct: int
    session_id: str | None
    cost_usd: float | None = None
    turn_s: float | None = None  # the running or the last turn (None: none yet)
    update: str = ""


class Palette:
    """Paints text in a colour role (karox6.tui_theme): ``paint("accent", "x")``. Roles
    combine with "+" (``"success+add_bg"``). Without a theme it uses the terminal's 16
    colours, as the chat always did."""

    def __init__(self, enabled: bool, theme: Theme | None = None) -> None:
        self.enabled = enabled
        self.theme = theme if theme is not None else Theme("ansi", 4 if enabled else 0)
        self.mark_frames = False  # the full screen sets it: it must know what is decoration

    def __call__(self, style: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        code = ";".join(c for c in (self.theme.sgr(s) for s in style.split("+")) if c)
        return f"\x1b[{code}m{text}\x1b[0m" if code else text

    def frame(self, text: str) -> str:
        """``text`` (a rule, a quote's bar, a box edge) in the ``subtle`` role. Where the
        screen copies text out of the conversation it is marked as decoration, which the
        copy leaves out; text that merely contains such characters is not marked."""
        shown = self("subtle", text)
        if self.mark_frames and self.enabled and text:
            return f"{DECO_START}{shown}{DECO_END}"
        return shown


_RICH = {
    "tool": "●",
    "ok": "✓",
    "fail": "✗",
    "prompt": "› ",
    "more": "… ",
    "dot": " · ",
    "result": "└ ",
    "pipe": "│ ",
    "bullet": "•",
    "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "bar": "▰▱",
    "arrow": "→",
    "up": "↑",
    "warn": "!",
}
# The Windows console without Windows Terminal draws with Consolas or Lucida Console and
# no font fallback: only the WGL4 character set is safe there.
_BASIC = {**_RICH, "ok": "√", "fail": "×", "spin": "·•●•", "bar": "█░"}
_PLAIN = {
    "tool": "*",
    "ok": "ok",
    "fail": "x",
    "prompt": "> ",
    "more": ". ",
    "dot": " | ",
    "result": "`-",
    "pipe": "| ",
    "bullet": "-",
    "spin": "|/-\\",
    "bar": "#-",
    "arrow": "->",
    "up": "^",
    "warn": "!",
}


def legacy_console(env: dict[str, str] | None = None, platform: str | None = None) -> bool:
    """A classic Windows console window (conhost), not Windows Terminal, VS Code, ConEmu
    or a terminal emulator that sets TERM: its fonts lack most symbols."""
    env = dict(os.environ) if env is None else env
    if (platform or sys.platform) != "win32":
        return False
    hints = ("WT_SESSION", "TERM_PROGRAM", "TERM", "ConEmuANSI", "ALACRITTY_LOG", "WEZTERM_PANE")
    return not any(env.get(name) for name in hints)


def _glyphs(encoding: str | None, legacy: bool = False) -> dict[str, str]:
    fancy = dict(_BASIC if legacy else _RICH)
    try:
        "".join(fancy.values()).encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return {**_PLAIN, "box": "ascii"}
    try:
        "".join(_BOX_FANCY).encode(encoding or "ascii")
        fancy["box"] = "fancy"
    except (LookupError, UnicodeEncodeError):
        fancy["box"] = "ascii"
    return fancy


def _plain_text(text: str) -> str:
    # Keep newlines and tabs. Model and tool output must never move the terminal cursor.
    return _TERMINAL_CONTROL.sub("", text)


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _git_branch(workspace: Path) -> str:
    """The checked-out branch read from .git/HEAD (no git process: it runs before the first
    prompt); empty when the project is not a git checkout or HEAD is detached."""
    for folder in (workspace, *workspace.parents):
        head = folder / ".git"
        if head.is_file():  # a worktree: "gitdir: <path>"
            try:
                target = head.read_text(encoding="utf-8").split(":", 1)[1].strip()
            except (OSError, IndexError, UnicodeDecodeError):
                return ""
            head = Path(target) if Path(target).is_absolute() else folder / target
        if head.is_dir():
            try:
                ref = (head / "HEAD").read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                return ""
            return ref.removeprefix("ref: refs/heads/") if ref.startswith("ref: ") else ""
    return ""


def narrow(text: str) -> bool:
    """Every character of ``text`` surely takes one terminal cell (Latin, Cyrillic, Greek,
    box drawing and the UI's own symbols), so its width is ``len(text)`` with no width
    tables. Anything else (CJK, emoji, combining marks) answers False."""
    if text.isascii():
        return text.isprintable() or not text
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code < 0xA0:
            return False
        if code < 0x300 or 0x370 <= code <= 0x52F or 0x2500 <= code <= 0x25FF:
            continue  # Latin, Greek, Cyrillic; box drawing, blocks, geometric shapes
        if 0x2190 <= code <= 0x21FF or 0x2800 <= code <= 0x28FF:
            continue  # arrows; braille (the spinner)
        if ch in "✓✗›…—–•√":
            continue
        return False
    return True


def cell_width(text: str) -> int:
    """Terminal cells ``text`` takes (wide characters count twice)."""
    if narrow(text):
        return len(text)
    from prompt_toolkit.utils import get_cwidth

    return sum(max(get_cwidth(ch), 0) for ch in text)


def clip_cells(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` cells, ending in an ellipsis when it was cut."""
    if limit <= 0:
        return ""
    if cell_width(text) <= limit:
        return text
    if narrow(text):
        return text[: limit - 1] + ELLIPSIS
    from prompt_toolkit.utils import get_cwidth

    out, used = [], 0
    for ch in text:
        width = max(get_cwidth(ch), 0)
        if used + width > limit - 1:
            break
        out.append(ch)
        used += width
    return "".join(out) + ELLIPSIS


def _clip_painted(text: str, limit: int) -> tuple[str, int]:
    """A string that may hold colour codes, cut to ``limit`` visible cells: (text, cells).
    Colour codes are kept whole and the colour is reset after a cut."""
    plain = _ANSI.sub("", text)
    width = cell_width(plain)
    if width <= limit:
        return text, width
    out: list[str] = []
    used = 0
    room = max(limit - 1, 0)
    pos = 0
    for match in _ANSI.finditer(text):
        chunk = text[pos : match.start()]
        take = clip_cells(chunk, room - used + 1) if cell_width(chunk) > room - used else chunk
        if take != chunk:
            out.append(take.removesuffix(ELLIPSIS))
            used = room
            break
        out.append(chunk)
        used += cell_width(chunk)
        out.append(match.group(0))
        pos = match.end()
    else:
        chunk = text[pos:]
        if cell_width(chunk) > room - used:
            chunk = clip_cells(chunk, room - used + 1).removesuffix(ELLIPSIS)
        out.append(chunk)
    cut = "".join(out)
    reset = "\x1b[0m" if "\x1b[" in cut else ""
    return cut + reset + ELLIPSIS, cell_width(_ANSI.sub("", cut)) + 1


def _box(
    title: str,
    rows: list[tuple[str, str]],
    fancy: bool,
    max_width: int | None = None,
    paint: Palette | None = None,
) -> str:
    """A framed panel: label column, then values (ANSI colors do not count as width).
    With ``max_width`` (terminal cells) longer rows are cut with an ellipsis, so the frame
    never wraps on a narrow terminal. ``paint`` colours the frame (role ``subtle``) and the
    labels (``muted``)."""
    tl, tr, bl, br, h, v = _BOX_FANCY if fancy else _BOX_ASCII
    frame = paint.frame if paint is not None else (lambda text: text)
    label = (lambda text: paint("muted", text)) if paint is not None else (lambda text: text)
    wide = max((len(k) for k, _ in rows), default=0)
    lines = [(label(f"{k:<{wide}}") + f"  {val}" if wide else val) for k, val in rows]
    seen = [cell_width(_ANSI.sub("", ln)) for ln in lines]
    shown = cell_width(_ANSI.sub("", title))
    width = max([*seen, shown + 2]) + 2
    if max_width is not None and width + 2 > max_width:
        width = max(max_width - 2, 8)
        clipped = [_clip_painted(ln, width - 2) for ln in lines]
        lines = [text for text, _ in clipped]
        seen = [cells for _, cells in clipped]
        if shown + 3 > width:
            title, shown = _clip_painted(title, width - 3)
    out = [frame(tl + h) + f" {title} " + frame(h * (width - shown - 3) + tr)]
    out += [
        f"{frame(v)} {ln}{' ' * (width - n - 1)}{frame(v)}"
        for ln, n in zip(lines, seen, strict=True)
    ]
    out.append(frame(bl + h * width + br))
    return "\n".join(out)


def short_path(path: str, home: str | None = None) -> str:
    """``path`` with the user's home folder written as ``~``."""
    home = home if home is not None else os.path.expanduser("~")
    if home and len(path) > len(home) and path.lower().startswith(home.lower()):
        rest = path[len(home) :]
        if rest[:1] in ("/", "\\"):
            return "~" + rest
    return path


def banner_text(
    *,
    version: str,
    model: str,
    provider: str,
    workspace: str,
    branch: str,
    approval_mode: str,
    paint: Palette,
    glyph: dict[str, str],
    max_width: int | None = None,
) -> str:
    """The welcome card: what runs where, and the keys a new user needs first."""
    dot = glyph["dot"]
    project = short_path(_plain_text(workspace))
    if branch:
        project += paint("muted", f"{dot}{_plain_text(branch)}")

    def keys(*pairs: tuple[str, str]) -> str:
        return paint("muted", dot).join(
            paint("accent", key) + paint("muted", f" {what}") for key, what in pairs
        )

    mode = paint({"allow": "warning", "deny": "error"}.get(approval_mode, ""), approval_mode)
    rows = [
        ("model", paint("bold", _plain_text(model)) + paint("muted", f"{dot}{provider}")),
        ("project", project),
        ("approvals", mode + paint("muted", "  (/approve to change)")),
        ("", ""),
        ("", keys(("/", "commands"), ("@", "attach a file"), ("Alt+Enter", "new line"))),
        ("", keys(("Esc", "stops the agent"), ("Ctrl+O", "tool output"), ("Ctrl+D", "quits"))),
    ]
    fancy = glyph.get("box", "ascii") == "fancy"
    title = paint("brand", "KaroX") + " " + paint("muted", version)
    card = _box(title, rows, fancy, max_width, paint if paint.enabled else None)
    return card + "\n" + paint("muted", "Type a task to start.") + "\n"


Fragments = list[tuple[str, str]]  # (colour role(s), text); "" is the default colour


def status_bar_fragments(
    snap: StatusSnapshot,
    glyph: dict[str, str],
    width: int,
) -> Fragments:
    """The status bar as (role, text) pieces that fill exactly ``width`` cells: model,
    project (branch) and a newer version on the left; context fill, cost, the turn's time
    and the approval mode on the right. On a narrow terminal the least telling parts go
    first (cost, time, the branch, the project, the fill gauge, the context), then the
    model name is cut: the model and the approval mode always stay."""
    dot = glyph["dot"]
    model = _plain_text(snap.model) or "setup"
    project = _plain_text(snap.workspace_name)
    branch = _plain_text(snap.branch)
    bar = glyph.get("bar", "") if len(glyph.get("bar", "")) == 2 else "#-"
    full, empty = bar[0], bar[1]
    pct = max(0, min(snap.context_pct, 100))
    filled = round(pct / 20) if pct < 100 else 5
    gauge_role = "success" if pct < 60 else "warning" if pct < 85 else "error"
    mode_role = {"allow": "warning", "deny": "error"}.get(snap.approval_mode, "")
    cost = f"${snap.cost_usd:.2f}" if snap.cost_usd is not None else ""
    turn = f"{snap.turn_s:.1f}s" if snap.turn_s is not None else ""

    def build(level: int, clip: int = 0) -> tuple[Fragments, Fragments]:
        left: Fragments = [("", " "), ("accent+bold", clip_cells(model, clip) if clip else model)]
        if level < 4 and project:
            shown = f"{project} ({branch})" if level < 3 and branch else project
            left.append(("muted", f"{dot}{shown}"))
        if snap.update and level < 5:
            left += [("muted", dot), ("warning", f"{glyph.get('up', '^')} {snap.update} /update")]
        right: Fragments = []
        if snap.context_window and level < 6:
            right.append(("muted", "ctx "))
            if level < 5:
                right += [(gauge_role, full * filled), ("subtle", empty * (5 - filled)), ("", " ")]
            right += [("", f"{pct}%"), ("muted", dot)]
        if cost and level < 1:
            right += [("", cost), ("muted", dot)]
        if turn and level < 2:
            right += [("muted", turn), ("muted", dot)]
        if level < 3:
            right.append(("muted", "approvals "))
        right += [(mode_role, snap.approval_mode), ("", " ")]
        return left, right

    def cells(fragments: Fragments) -> int:
        return sum(cell_width(text) for _, text in fragments)

    left: Fragments = []
    right: Fragments = []
    for level in range(7):
        left, right = build(level)
        if cells(left) + cells(right) + 1 <= width:
            break
    else:
        spare = width - cells(right) - 2
        left, right = build(6, clip=max(spare, 1))
    gap = max(width - cells(left) - cells(right), 1)
    return [*left, ("", " " * gap), *right]


def key_hints(mode: str) -> list[tuple[str, str]]:
    """The keys worth knowing right now, for the frame of the input box."""
    if mode == "running":
        return [("esc", "stop"), ("enter", "queue a message"), ("ctrl+o", "tool output")]
    if mode == "question":
        return [("enter", "answer"), ("esc", "cancel")]
    if mode == "secret":
        return [("enter", "submit"), ("esc", "cancel")]
    return [
        ("enter", "send"),
        ("alt+enter", "new line"),
        ("/", "commands"),
        ("@", "files"),
        ("ctrl+o", "tool output"),
        ("drag", "copy"),
    ]


def box_top(width: int, glyph: dict[str, str]) -> Fragments:
    tl, tr, _bl, _br, h, _v = _BOX_FANCY if glyph.get("box") == "fancy" else _BOX_ASCII
    return [("subtle", tl + h * max(width - 2, 0) + tr)]


def box_bottom(width: int, glyph: dict[str, str], hints: list[tuple[str, str]]) -> Fragments:
    """The lower edge of the input box with ``hints`` (key, what) set into it on the
    right; hints that do not fit are left out from the end."""
    _tl, _tr, bl, br, h, _v = _BOX_FANCY if glyph.get("box") == "fancy" else _BOX_ASCII
    dot = glyph["dot"]
    shown = list(hints)
    while shown:
        text = dot.join(f"{k} {w}" for k, w in shown)
        if cell_width(text) + 6 <= width:
            break
        shown.pop()
    if not shown:
        return [("subtle", bl + h * max(width - 2, 0) + br)]
    middle: Fragments = [("", " ")]
    for i, (key, what) in enumerate(shown):
        if i:
            middle.append(("subtle", dot))
        middle += [("muted+bold", key), ("muted", f" {what}")]
    middle.append(("", " "))
    used = sum(cell_width(t) for _, t in middle)
    fill = max(width - used - 3, 0)
    return [("subtle", bl + h * fill), *middle, ("subtle", h + br)]


def box_sides(glyph: dict[str, str]) -> tuple[str, str]:
    v = (_BOX_FANCY if glyph.get("box") == "fancy" else _BOX_ASCII)[5]
    return v + " ", " " + v
