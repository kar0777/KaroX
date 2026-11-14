"""Decoding and line splitting shared by the read, search and edit tools."""

from __future__ import annotations

LATIN_1 = "latin-1"


def decode_source(raw: bytes) -> tuple[str, str]:
    """The file's text and the encoding that writes it back byte for byte.

    UTF-8 first. A file that is not UTF-8 but holds no NUL byte is read as
    Latin-1, which maps every byte to one character and back, so an edit that
    adds only Latin-1 characters leaves every untouched byte as it was. Such
    files (cp1252 comments, Latin-1 fixtures) used to be refused by every
    read and edit tool, which left shell one-liners as the only way in.
    Binary data (a NUL byte) still raises UnicodeDecodeError.
    """

    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        if b"\x00" in raw:
            raise
        return raw.decode(LATIN_1), LATIN_1


def physical_lines(text: str, keepends: bool = False) -> list[str]:
    """Lines split at "\\n" only, like grep, tracebacks and editors count them.

    str.splitlines also breaks at \\f, \\v, \\x1c-\\x1e, \\x85, U+2028 and
    U+2029, so a form feed between functions or a U+2028 inside a string
    literal shifted every later line number away from grep, tracebacks and
    the line numbers the model edits by. Without ``keepends`` a line loses its
    "\\n" and a "\\r" just before it; a lone "\\r" elsewhere is content.
    """

    pieces = text.split("\n")
    tail = pieces.pop()
    if keepends:
        lines = [piece + "\n" for piece in pieces]
    else:
        lines = [piece[:-1] if piece.endswith("\r") else piece for piece in pieces]
    if tail:
        lines.append(tail)
    return lines


__all__ = ["LATIN_1", "decode_source", "physical_lines"]
