"""Condense a long test-runner log before the model reads it.

A green or red pytest run in a large repository prints hundreds of progress
lines (dots, `u`, `s`) and, when it fails, the useful part is a few sections:
the FAILURES/ERRORS blocks, the short summary and the final count. Sending the
whole log costs input tokens on every later request and pushes the useful lines
into the clipped middle. This keeps exactly the parts a fix needs.

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
#: When a parsed failure map already names every failure, the raw log is only
#: corroboration. Keep a short slice of it instead of the full 12 KB.
CONDENSED_MAX_CHARS_WITH_MAP = 2_500

_PROGRESS_LINE = re.compile(r"^[.FEsxXuU]+\s*(\[\s*\d+%\])?\s*$")
_SECTION = re.compile(r"^=+ (.+?) =+$")
_KEEP_SECTIONS = ("test session starts", "FAILURES", "ERRORS", "short test summary info")
_FINAL = re.compile(
    r"\b\d+ (passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b"
)


def _looks_like_pytest(text: str) -> bool:
    return bool(_FINAL.search(text[-2_000:])) or "short test summary info" in text


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
    section: Optional[str] = None
    header_done = False
    for line in lines:
        match = _SECTION.match(line.strip())
        if match:
            title = match.group(1).strip()
            section = title
            header_done = True
            if any(name in title for name in _KEEP_SECTIONS) or _FINAL.search(title):
                kept.append(line)
            continue
        if _PROGRESS_LINE.match(line.strip()) and line.strip():
            progress_dropped += 1
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
    body = "\n".join(kept).strip("\n")
    if progress_dropped:
        body = f"[KaroX dropped {progress_dropped} progress lines]\n" + body
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
