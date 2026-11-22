"""Long pasted text held beside the input line.

A paste of a few lines goes into the input as it is; a big one is kept here and the
input shows a compact placeholder (``[pasted 5,000 lines · 300 KB]``) that the user
can type around or delete like any other text. When the message goes out, ``expand``
puts the real text back in place of every placeholder. This keeps a 300 KB paste out
of the line editor, so the input stays responsive.

Pure stdlib, no prompt_toolkit: the unit tests drive it without a terminal.
"""

from __future__ import annotations

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
    """The pasted payloads behind the placeholders currently in the input."""

    def __init__(self) -> None:
        self._chunks: dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(self._chunks)

    def offer(self, data: str) -> str:
        """What goes into the input for this paste: the text itself when small,
        else a placeholder whose payload is kept here."""
        if data.count("\n") < MIN_LINES and len(data.encode("utf-8")) < MIN_BYTES:
            return data
        shown = f"[pasted {pasted_summary(data)}]"
        serial = 2
        while shown in self._chunks:  # a paste of the same size is already waiting
            shown = f"[pasted {pasted_summary(data)} ·{serial}]"
            serial += 1
        self._chunks[shown] = data
        return shown

    def expand(self, text: str) -> str:
        """``text`` with every placeholder replaced by its payload (longest first,
        so a numbered one is seen whole). A half-deleted placeholder stays literal."""
        for shown in sorted(self._chunks, key=len, reverse=True):
            text = text.replace(shown, self._chunks[shown])
        return text

    def clear(self) -> None:
        self._chunks.clear()
