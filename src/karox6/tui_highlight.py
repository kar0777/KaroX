"""Light syntax colouring for code blocks in the chat: one regular expression per family
of languages, a line at a time (no dependency, imported only when a block shows up).

It colours what helps reading at a glance (comments, strings, numbers, keywords, calls)
and leaves the rest in the terminal's own colour; it is not a parser, so a string that
spans lines is coloured on its first line only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import cache

Paint = Callable[[str, str], str]

_KEYWORDS: dict[str, str] = {
    "python": "and as assert async await break class continue def del elif else except "
    "finally for from global if import in is lambda match case nonlocal not or pass raise "
    "return try while with yield None True False self",
    "js": "async await break case catch class const continue debugger default delete do "
    "else export extends finally for from function if import in instanceof let new of "
    "return static super switch this throw try typeof var void while yield null undefined "
    "true false interface type enum implements private public protected readonly as",
    "shell": "if then else elif fi for while until do done case esac in function return "
    "local export set unset echo cd source exit param foreach try catch",
    "rust": "as async await break const continue crate dyn else enum extern false fn for "
    "if impl in let loop match mod move mut pub ref return self Self static struct super "
    "trait true type unsafe use where while Some None Ok Err",
    "go": "break case chan const continue default defer else fallthrough for func go goto "
    "if import interface map package range return select struct switch type var nil true "
    "false",
    "c": "auto break case char const continue default do double else enum extern float for "
    "goto if inline int long register return short signed sizeof static struct switch "
    "typedef union unsigned void volatile while class public private protected new delete "
    "namespace using template typename virtual override final bool true false null nullptr "
    "this throw try catch import package extends implements interface var val fun when "
    "string let func",
    "sql": "select from where insert into values update set delete create table drop alter "
    "add index join left right inner outer on group by order having limit offset as and or "
    "not null is in exists distinct union all primary key foreign references default case "
    "when then else end",
    "data": "true false null yes no on off",
}
_FAMILY = {
    "python": "python",
    "py": "python",
    "python3": "python",
    "js": "js",
    "javascript": "js",
    "jsx": "js",
    "ts": "js",
    "typescript": "js",
    "tsx": "js",
    "mjs": "js",
    "sh": "shell",
    "bash": "shell",
    "shell": "shell",
    "zsh": "shell",
    "console": "shell",
    "powershell": "shell",
    "ps1": "shell",
    "pwsh": "shell",
    "bat": "shell",
    "cmd": "shell",
    "rust": "rust",
    "rs": "rust",
    "go": "go",
    "golang": "go",
    "c": "c",
    "h": "c",
    "cpp": "c",
    "c++": "c",
    "cc": "c",
    "hpp": "c",
    "java": "c",
    "cs": "c",
    "csharp": "c",
    "kotlin": "c",
    "kt": "c",
    "swift": "c",
    "scala": "c",
    "php": "c",
    "sql": "sql",
    "json": "data",
    "jsonc": "data",
    "yaml": "data",
    "yml": "data",
    "toml": "data",
    "ini": "data",
    "cfg": "data",
}
_HASH_COMMENT = {"python", "shell", "data"}
_SLASH_COMMENT = {"js", "rust", "go", "c"}


@cache
def _pattern(family: str) -> re.Pattern[str]:
    words = "|".join(sorted(map(re.escape, _KEYWORDS[family].split()), key=len, reverse=True))
    flags = re.IGNORECASE if family == "sql" else 0
    comment = []
    if family in _HASH_COMMENT:
        comment.append(r"#.*$" if family != "data" else r"(?:^|\s)[#;].*$")
    if family in _SLASH_COMMENT:
        comment.append(r"//.*$|/\*.*?(?:\*/|$)")
    if family == "sql":
        comment.append(r"--.*$")
    parts = [
        rf"(?P<comment>{'|'.join(comment)})" if comment else "",
        r"(?P<string>[rbfuRBFU]{0,2}\"\"\".*?(?:\"\"\"|$)|'''.*?(?:'''|$)"
        r"|\"(?:\\.|[^\"\\])*\"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?)",
        r"(?P<deco>^\s*@[\w.]+)" if family == "python" else "",
        r"(?P<number>\b(?:0[xob][\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)",
        rf"(?P<keyword>\b(?:{words})\b)",
        r"(?P<func>\b[A-Za-z_]\w*(?=\())",
        r"(?P<key>^\s*[\w.\-\"]+(?=\s*[:=]))" if family == "data" else "",
    ]
    return re.compile("|".join(p for p in parts if p), flags)


_ROLE = {
    "comment": "comment",
    "string": "string",
    "deco": "func",
    "number": "number",
    "keyword": "keyword",
    "func": "func",
    "key": "accent",
}


def language(name: str) -> str | None:
    """The family a fence's language name belongs to (None: not coloured)."""
    lang = name.strip().lower().split()[0] if name.strip() else ""
    if lang in {"diff", "patch"}:
        return "diff"
    return _FAMILY.get(lang)


def highlight(line: str, family: str | None, paint: Paint) -> str:
    """One line of code with colour codes for its tokens."""
    if family is None or not line.strip():
        return line
    if family == "diff":
        if line.startswith("+") and not line.startswith("+++"):
            return paint("success", line)
        if line.startswith("-") and not line.startswith("---"):
            return paint("error", line)
        if line.startswith("@@"):
            return paint("accent", line)
        return paint("muted", line) if line.startswith(("diff ", "index ", "---", "+++")) else line
    out: list[str] = []
    pos = 0
    for match in _pattern(family).finditer(line):
        kind = match.lastgroup
        if kind is None or match.start() == match.end():
            continue
        out.append(line[pos : match.start()])
        out.append(paint(_ROLE[kind], match.group(0)))
        pos = match.end()
    out.append(line[pos:])
    return "".join(out)
