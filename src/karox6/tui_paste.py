"""Long pasted text held beside the input line.

A paste of a few lines goes into the input as it is; a big one is kept here and the
input shows a compact placeholder (``[pasted 5,000 lines · 300 KB]``) that the user
can type around or delete like any other text. When the message goes out, ``expand``
puts the real text back in place of every placeholder. This keeps a 300 KB paste out
of the line editor, so the input stays responsive.

Pure stdlib, no prompt_toolkit: the unit tests drive it without a terminal.
"""

from __future__ import annotations

import re

MIN_LINES = 10  # a paste with more lines than this collapses to a placeholder
MIN_BYTES = 4096  # and so does one with more bytes
WARN_BYTES = 2 * 1024 * 1024  # pastes this large ask for a second Enter


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def pasted_summary(data: str) -> str:
    """``5,000 lines · 300 KB`` — what a placeholder says about the real paste."""
    lines = data.count("\n") + 1
    word = "line" if lines == 1 else "lines"
    return f"{lines:,} {word} · {_fmt_size(len(data.encode('utf-8')))}"


class PasteChunks:
    """The pasted payloads behind the placeholders of the draft being typed. The
    owner clears it whenever the draft ends (sent, cancelled), so a label typed later
    cannot bring an old paste back."""

    def __init__(self) -> None:
        self._chunks: dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(self._chunks)

    def offer(self, data: str) -> str:
        """What goes into the input for this paste: the text itself when small,
        else a placeholder whose payload is kept here. Text that spells out a label
        already in the draft is kept behind a placeholder too, so it stays literal."""
        small = data.count("\n") < MIN_LINES and len(data.encode("utf-8")) < MIN_BYTES
        if small and not any(shown in data for shown in self._chunks):
            return data
        shown = f"[pasted {pasted_summary(data)}]"
        serial = 2
        while shown in self._chunks:  # a paste of the same size is already waiting
            shown = f"[pasted {pasted_summary(data)} ·{serial}]"
            serial += 1
        self._chunks[shown] = data
        return shown

    def forget(self, shown: str) -> None:
        """Drop the payload behind ``shown`` (a paste that grew replaces its label)."""
        self._chunks.pop(shown, None)

    def expand(self, text: str) -> str:
        """``text`` with every placeholder replaced by its payload, in one pass: a
        payload is never searched for further labels, so a pasted text that holds
        one stays as pasted. A half-deleted placeholder stays literal."""
        if not self._chunks:
            return text
        labels = sorted(self._chunks, key=len, reverse=True)
        return re.sub("|".join(map(re.escape, labels)), lambda m: self._chunks[m.group()], text)

    def clear(self) -> None:
        self._chunks.clear()
