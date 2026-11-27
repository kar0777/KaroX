"""The list picker of the full-screen chat: arrows move, typing filters, Enter chooses.

Used for the model (``/model``), the saved providers (``/provider``) and the setup. The
state is plain data so it can be tested without a terminal; ``FullScreenTui`` draws it
and feeds it keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Fragments = list[tuple[str, str]]

ROWS = 12  # list rows shown at once


@dataclass
class PickState:
    title: str
    items: list[str]
    current: str | None = None  # marked, and where the cursor starts
    allow_typed: bool = True  # Enter on a query that matches nothing returns the query
    notes: dict[str, str] = field(default_factory=dict)  # shown dim beside an item
    query: str = ""
    index: int = 0
    top: int = 0
    _shown: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.set_query("")

    def filtered(self) -> list[str]:
        return self._shown

    def set_query(self, query: str) -> None:
        self.query = query
        words = query.lower().split()
        self._shown = [i for i in self.items if all(w in i.lower() for w in words)]
        self.index = 0
        if not words and self.current in self._shown:
            self.index = self._shown.index(self.current)
        self.top = 0
        self._keep_visible()

    def move(self, delta: int) -> None:
        if not self._shown:
            return
        self.index = max(0, min(len(self._shown) - 1, self.index + delta))
        self._keep_visible()

    def home(self, end: bool = False) -> None:
        self.index = len(self._shown) - 1 if end and self._shown else 0
        self._keep_visible()

    def chosen(self) -> str | None:
        if self._shown:
            return self._shown[self.index]
        query = self.query.strip()
        return query if query and self.allow_typed else None

    def _keep_visible(self) -> None:
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + ROWS:
            self.top = self.index - ROWS + 1
        self.top = max(0, min(self.top, max(len(self._shown) - ROWS, 0)))

    def rows(self) -> int:
        return max(1, min(ROWS, len(self._shown)))

    def header(self, dot: str) -> Fragments:
        count = f"{len(self._shown)} of {len(self.items)}" if self.query else f"{len(self.items)}"
        keys = f"↑↓ move{dot}type to filter{dot}enter choose{dot}esc cancel"
        return [("class:pick-bar", f" {self.title} ({count}) "), ("class:pick-keys", keys)]

    def lines(self, pointer: str = "❯", mark: str = "●") -> Fragments:
        """The visible rows: the cursor row highlighted, the current item marked, the
        typed words emphasised; a hint when nothing matches."""
        if not self._shown:
            if self.query.strip() and self.allow_typed:
                return [("class:pick-dim", f"  nothing matches; enter uses “{self.query.strip()}”")]
            return [("class:pick-dim", "  nothing matches")]
        out: Fragments = []
        words = [w for w in self.query.lower().split() if w]
        end = min(self.top + ROWS, len(self._shown))
        visible = self._shown[self.top : end]
        pad = max(len(i) + (2 if i == self.current else 0) for i in visible) + 2
        for n in range(self.top, end):
            item = self._shown[n]
            here = n == self.index
            row = "class:pick-current" if here else "class:pick-item"
            out.append((row, f" {pointer} " if here else "   "))
            out += _emphasised(item, words, row)
            used = len(item)
            if item == self.current:
                out.append((f"{row} class:pick-mark", f" {mark}"))
                used += 2
            note = self.notes.get(item)
            if note:
                out.append((f"{row} class:pick-note", " " * (pad - used) + note))
            more = ""
            if n == self.top and self.top > 0:
                more = "  ↑ more"
            elif n == end - 1 and end < len(self._shown):
                more = f"  ↓ {len(self._shown) - end} more"
            if more:
                out.append(("class:pick-dim", more))
            if n != end - 1:
                out.append(("", "\n"))
        return out


def _emphasised(text: str, words: list[str], style: str) -> Fragments:
    """``text`` with every typed word shown in bold."""
    low = text.lower()
    marks = [False] * len(text)
    for w in words:
        start = low.find(w)
        while start != -1:
            for i in range(start, start + len(w)):
                marks[i] = True
            start = low.find(w, start + len(w))
    out: Fragments = []
    for ch, bold in zip(text, marks, strict=True):
        s = f"{style} class:pick-match" if bold else style
        if out and out[-1][0] == s:
            out[-1] = (s, out[-1][1] + ch)
        else:
            out.append((s, ch))
    return out
