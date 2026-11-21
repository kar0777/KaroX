"""Text of the chat UI that needs no event loop and no terminal library: colours, glyphs,
the welcome card and the status bar.

It is shared by the chat (``tui``), the full-screen app (``tui_app``) and the start-up
frame (``splash``), which paints the first screen before asyncio and prompt_toolkit are
even imported; one source keeps that frame identical to the app's own first frame.
Imports here stay cheap on purpose (no asyncio, no prompt_toolkit at module level).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
_BOX_FANCY = ("╭", "╮", "╰", "╯", "─", "│")
_BOX_ASCII = ("+", "+", "+", "+", "-", "|")
_ANSI = re.compile("\x1b\\[[0-9;]*m")
_TERMINAL_CONTROL = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)
ELLIPSIS = "…"


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


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, style: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\x1b[{_CODES[style]}m{text}\x1b[0m"


def _glyphs(encoding: str | None) -> dict[str, str]:
    fancy = {"tool": "●", "ok": "✓", "fail": "✗", "prompt": "› ", "more": "… ", "dot": " · "}
    try:
        "".join(fancy.values()).encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return {"tool": "*", "ok": "ok", "fail": "x", "prompt": "> ", "more": ". ", "dot": " | "}
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
        if code < 0x300 or 0x370 <= code <= 0x52F or 0x2500 <= code <= 0x257F:
            continue
        if ch in "●✓✗›…—–":
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


def _box(title: str, rows: list[tuple[str, str]], fancy: bool, max_width: int | None = None) -> str:
    """A framed panel: label column, then values (ANSI colors do not count as width).
    With ``max_width`` (terminal cells) longer rows are cut with an ellipsis, so the frame
    never wraps on a narrow terminal."""
    tl, tr, bl, br, h, v = _BOX_FANCY if fancy else _BOX_ASCII
    wide = max((len(k) for k, _ in rows), default=0)
    lines = [(f"{k:<{wide}}  {val}" if wide else val) for k, val in rows]
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
    out = [tl + h + f" {title} " + h * (width - shown - 3) + tr]
    out += [f"{v} {ln}{' ' * (width - n - 1)}{v}" for ln, n in zip(lines, seen, strict=True)]
    out.append(bl + h * width + br)
    return "\n".join(out)


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
    project = _plain_text(workspace)
    if branch:
        project += paint("dim", f"{dot}{_plain_text(branch)}")
    rows = [
        ("model", _plain_text(model) + paint("dim", f"{dot}{provider}")),
        ("project", project),
        ("approvals", f"{approval_mode}" + paint("dim", "  (/approve to change)")),
        ("", ""),
        ("", paint("dim", f"/ commands{dot}@ attach a file{dot}Alt+Enter new line")),
        ("", paint("dim", f"Esc stops the agent{dot}/help{dot}Ctrl+D quits")),
    ]
    fancy = glyph.get("box", "ascii") == "fancy"
    card = _box(paint("bold", f"KaroX {version}"), rows, fancy, max_width)
    return card + "\n" + paint("dim", "Type a task to start.") + "\n"


Fragments = list[tuple[str, str]]  # (style name, text); "" is the bar's own style


def status_bar_fragments(
    snap: StatusSnapshot,
    glyph: dict[str, str],
    width: int,
    *,
    scrolled: int = 0,
    queued: int = 0,
) -> Fragments:
    """The status bar as (style, text) pieces that fill exactly ``width`` cells: model,
    project and live activity on the left; tokens, context fill and the approval mode on
    the right. On a narrow terminal the least telling parts go first (session tokens, the
    branch, the project, the context fill), and the activity text is cut last: the model
    and the approval mode always stay."""
    dot = glyph["dot"]
    model = _plain_text(snap.model) or "setup"
    project = _plain_text(snap.workspace_name)
    branch = _plain_text(snap.branch)
    note: tuple[str, str] | None = None
    if snap.running and snap.activity:
        note = ("activity", _plain_text(snap.activity))
    elif scrolled > 0:
        note = ("scroll", f"scrolled +{scrolled}L (End)")
    waiting = f"{queued} queued (Esc clears)" if queued else ""
    ctx = (
        f"ctx {_k(snap.context_used)}/{_k(snap.context_window)} ({snap.context_pct}%)"
        if snap.context_window
        else ""
    )
    tokens = f"{_k(snap.total_tokens)} tok" if snap.total_tokens and ctx else ""
    mode_style = {"allow": "allow", "deny": "deny"}.get(snap.approval_mode, "dim")
    mode = f"approve:{snap.approval_mode}"

    def build(
        *, with_tokens: bool, with_branch: bool, with_project: bool, with_ctx: bool, clip: int
    ) -> tuple[Fragments, Fragments]:
        left: Fragments = [("", " "), ("model", model)]
        if with_project and project:
            shown = f"{project} ({branch})" if with_branch and branch else project
            left.append(("dim", f"{dot}{shown}"))
        if note is not None:
            text = clip_cells(note[1], clip) if clip else note[1]
            left += [("dim", dot), (note[0], text)]
        if waiting:
            left += [("dim", dot), ("scroll", waiting)]
        right: Fragments = []
        parts = [p for p, keep in ((tokens, with_tokens), (ctx, with_ctx)) if p and keep]
        if parts:
            right.append(("dim", dot.join(parts) + dot))
        right += [(mode_style, mode), ("", " ")]
        return left, right

    def cells(fragments: Fragments) -> int:
        return sum(cell_width(text) for _, text in fragments)

    variants = (
        {"with_tokens": True, "with_branch": True, "with_project": True, "with_ctx": True},
        {"with_tokens": False, "with_branch": True, "with_project": True, "with_ctx": True},
        {"with_tokens": False, "with_branch": False, "with_project": True, "with_ctx": True},
        {"with_tokens": False, "with_branch": False, "with_project": False, "with_ctx": True},
        {"with_tokens": False, "with_branch": False, "with_project": False, "with_ctx": False},
    )
    left: Fragments = []
    right: Fragments = []
    for options in variants:
        left, right = build(clip=0, **options)
        if cells(left) + cells(right) + 1 <= width:
            break
    else:
        # Still too wide: cut the activity text, then the model name, to what is left.
        fixed = cells(build(clip=1, **variants[-1])[0]) + cells(right) + 1
        spare = max(width - fixed + 1, 1)
        left, right = build(clip=spare, **variants[-1])
        over = cells(left) + cells(right) + 1 - width
        if over > 0:
            short = clip_cells(model, max(cell_width(model) - over, 1))
            left = [(style, short if text == model else text) for style, text in left]
    gap = max(width - cells(left) - cells(right), 1)
    return [*left, ("", " " * gap), *right]
