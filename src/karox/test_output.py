"""Condense a long test-runner log before the model reads it.

A green or red pytest run in a large repository prints hundreds of progress
lines (dots, `u`, `s`, or one PASSED line per test with ``-v``) and, when it
fails, the useful part is a few sections: the FAILURES/ERRORS blocks, the
short summary and the final count. Sending the whole log costs input tokens on
every later request and pushes the useful lines into the clipped middle. This
keeps exactly the parts a fix needs.

Only the text the model reads is condensed. The stored result (and its
sha256/byte counts, which evidence is built from) is left untouched, so an
auditor still sees what really ran.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

#: Below this size a log is returned as-is: condensing it saves nothing.
CONDENSE_THRESHOLD_CHARS = 4_000
#: Upper bound for the condensed text; failure blocks are cut from the middle.
CONDENSED_MAX_CHARS = 12_000
#: When a parsed failure map already names every failure, the raw log is
#: corroboration plus the detail the map cannot hold (the assertion diff, the
#: failing line). Failure blocks are compacted first, so this keeps the
#: ``E`` lines of several failures rather than a slice of one.
CONDENSED_MAX_CHARS_WITH_MAP = 4_000
#: ``E`` lines kept per failure block, and captured-output lines per section.
E_LINES_PER_BLOCK = 40
CAPTURED_LINES_PER_SECTION = 20

_PROGRESS_LINE = re.compile(r"^[.FEsxXuU]+\s*(\[\s*\d+%\])?\s*$")
#: ``pytest -v`` prints one line per test; the outcome is in the summary.
_VERBOSE_RESULT = re.compile(
    r"^\S+::\S.*\s(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(\s+\[\s*\d+%\])?\s*$"
)
_SECTION = re.compile(r"^=+ (.+?) =+$")
_KEEP_SECTIONS = ("test session starts", "FAILURES", "ERRORS", "short test summary info")
_BLOCK_SECTIONS = ("FAILURES", "ERRORS")
_BLOCK_TITLE = re.compile(r"^_{3,} .+ _{3,}$")
_FRAME_SEPARATOR = re.compile(r"^(_ )+_?$")
_LOCATION = re.compile(r"^[^\s:][^:]*:\d+: \S")
_CAPTURED = re.compile(r"^-{3,} .+ -{3,}$")
_FINAL = re.compile(
    r"\b\d+ (passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b"
)


def _looks_like_pytest(text: str) -> bool:
    return bool(_FINAL.search(text[-2_000:])) or "short test summary info" in text


class _BlockCompactor:
    """Keeps what explains a failure: title, failing line, E lines, location.

    A pytest failure block prints the whole test function and every library
    frame's source. The cause is in the ``>`` line, the ``E`` lines under it
    and the ``path:line: Error`` locator; the rest is context the model can
    read with one call when it needs it.
    """

    def __init__(self) -> None:
        self.context: List[str] = []
        self.e_lines = 0
        self.e_dropped = 0
        self.captured = -1
        self.dropped = 0

    def _flush_e(self, kept: List[str]) -> None:
        if self.e_dropped:
            kept.append(f"E   [KaroX: {self.e_dropped} more E lines]")
            self.e_dropped = 0

    def feed(self, line: str, kept: List[str]) -> None:
        stripped = line.strip()
        if _BLOCK_TITLE.match(stripped):
            self._flush_e(kept)
            self.dropped += len(self.context)
            self.context, self.e_lines, self.captured = [], 0, -1
            kept.append(line)
            return
        if _CAPTURED.match(stripped):
            self._flush_e(kept)
            self.captured = 0
            kept.append(line)
            return
        if self.captured >= 0:
            if self.captured < CAPTURED_LINES_PER_SECTION:
                kept.append(line)
            else:
                self.dropped += 1
            self.captured += 1
            return
        if line.startswith("E ") or stripped == "E":
            if self.e_lines < E_LINES_PER_BLOCK:
                kept.append(line)
            else:
                self.e_dropped += 1
            self.e_lines += 1
            return
        if line.startswith(">"):
            kept.extend(self.context[-2:])
            self.dropped += max(0, len(self.context) - 2)
            self.context = []
            kept.append(line)
            return
        if _LOCATION.match(stripped):
            self._flush_e(kept)
            kept.append(line)
            self.dropped += len(self.context)
            self.context = []
            return
        if _FRAME_SEPARATOR.match(stripped):
            self.dropped += len(self.context)
            self.context = []
            return
        self.context.append(line)

    def close(self, kept: List[str]) -> None:
        self._flush_e(kept)
        self.dropped += len(self.context)
        self.context = []


def condense_test_output(
    text: str,
    *,
    max_chars: int = CONDENSED_MAX_CHARS,
    threshold: int = CONDENSE_THRESHOLD_CHARS,
) -> Optional[Dict[str, object]]:
    """Return {"text", "original_chars", "kept_chars"} or None when not worth it."""

    if not isinstance(text, str) or len(text) <= threshold:
        return None
    if not _looks_like_pytest(text):
        return None
    lines = text.splitlines()
    kept: List[str] = []
    progress_dropped = 0
    context_dropped = 0
    section: Optional[str] = None
    header_done = False
    blocks: Optional[_BlockCompactor] = None
    for line in lines:
        match = _SECTION.match(line.strip())
        if match:
            if blocks is not None:
                blocks.close(kept)
                context_dropped += blocks.dropped
                blocks = None
            title = match.group(1).strip()
            section = title
            header_done = True
            if any(name in title for name in _KEEP_SECTIONS) or _FINAL.search(title):
                kept.append(line)
            if any(name in title for name in _BLOCK_SECTIONS):
                blocks = _BlockCompactor()
            continue
        stripped = line.strip()
        if stripped and (_PROGRESS_LINE.match(stripped) or _VERBOSE_RESULT.match(stripped)):
            progress_dropped += 1
            continue
        if blocks is not None:
            blocks.feed(line, kept)
            continue
        if section is None and not header_done:
            # The session header (platform, rootdir, plugins, collected N items).
            kept.append(line)
            continue
        if section is not None and any(name in section for name in _KEEP_SECTIONS):
            kept.append(line)
            continue
        if _FINAL.search(line):
            kept.append(line)
    if blocks is not None:
        blocks.close(kept)
        context_dropped += blocks.dropped
    body = "\n".join(kept).strip("\n")
    notes = []
    if progress_dropped:
        notes.append(f"{progress_dropped} progress lines")
    if context_dropped:
        notes.append(f"{context_dropped} source-context lines of failure blocks")
    if notes:
        body = f"[KaroX dropped {' and '.join(notes)}]\n" + body
    if len(body) > max_chars:
        head = body[: (max_chars * 2) // 3]
        tail = body[-(max_chars // 3):]
        body = (
            f"{head}\n... [KaroX omitted {len(body) - len(head) - len(tail)} characters "
            "of failure detail; rerun the single failing test for the rest] ...\n"
            f"{tail}"
        )
    if len(body) >= len(text):
        return None
    return {"text": body, "original_chars": len(text), "kept_chars": len(body)}
