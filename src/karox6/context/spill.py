"""Inline limits for tool output; the full text goes to a spill file the model can read.

The notice wording follows DSH's spill policy (MIT, see THIRD_PARTY_NOTICES.md):
the model is told exactly where the full output lives and how to look inside it.
"""

from __future__ import annotations

import secrets
from pathlib import Path

DEFAULT_MAX_CHARS = 30_000
GAP = "\n\n[...]\n\n"


def spill_notice(total_chars: int, path: Path | None) -> str:
    where = str(path) if path is not None else "(unavailable)"
    return (
        f"(Output truncated: {total_chars} chars in total. Full output saved to: {where}. "
        "Use read with offset/limit, or grep this path to search within it.)"
    )


def write_spill(spill_dir: Path, name: str, text: str) -> Path | None:
    try:
        spill_dir.mkdir(parents=True, exist_ok=True)
        path = spill_dir / f"{secrets.token_hex(6)}-{_safe(name)}.txt"
        path.write_text(text, encoding="utf-8", newline="")
    except OSError:
        return None
    return path


def limit_output(
    text: str,
    *,
    spill_dir: Path,
    name: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    full_path: Path | None = None,
    head_ratio: float = 0.5,
) -> tuple[str, Path | None]:
    """Return (inline text, spill path or None).

    Text within ``max_chars`` passes through unchanged. Longer text keeps a head and
    a tail around a gap marker and ends with a notice naming the file that holds all
    of it (``full_path`` when the caller already has one, else a new spill file).
    """
    if len(text) <= max_chars:
        return text, None
    path = full_path if full_path is not None else write_spill(spill_dir, name, text)
    notice = spill_notice(len(text), path)
    budget = max(max_chars - len(notice) - len(GAP) - 2, 200)
    head_len = int(budget * head_ratio)
    tail_len = budget - head_len
    head = _cut_at_line(text[:head_len], from_end=False)
    tail = _cut_at_line(text[len(text) - tail_len :], from_end=True)
    return f"{head}{GAP}{tail}\n\n{notice}", path


def _cut_at_line(chunk: str, *, from_end: bool) -> str:
    """Trim a partial first/last line so the preview shows whole lines when possible."""
    if from_end:
        idx = chunk.find("\n")
        return chunk[idx + 1 :] if 0 <= idx < len(chunk) // 4 else chunk
    idx = chunk.rfind("\n")
    return chunk[:idx] if idx > len(chunk) * 3 // 4 else chunk


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:40] or "output"
