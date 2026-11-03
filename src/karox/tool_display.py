"""What one tool call is about, and what it returned, in display words.

The live event stream tells a watcher *which* tool runs. A reader following
the work needs two more facts: the call's target -- ``Read(src/app.py)``,
``Run(pytest -q)`` -- and the gist of its result -- ``lines=40``, ``+3 -1``,
the last lines a check printed. These helpers derive both from the call's
arguments and the CoreResult data the runtime already holds.

Display only, and deliberately conservative: every string is redacted with the
display form of :func:`karox.security.redact` and bounded; a write never echoes
the content it wrote, a browser ``type`` never echoes the text it typed, and a
URL loses its query string. The previews are a short diff or output tail, not
the payload: the durable session record stays the authority.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .security import redact

#: One call target fits one terminal line next to the tool's name.
DETAIL_LIMIT = 160
#: A preview is a glance, not the output: a dozen lines of a few columns each.
PREVIEW_LINES = 12
PREVIEW_LINE_LIMIT = 200
PREVIEW_LIMIT = 2400
PLAN_ITEMS = 12

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_COMMAND_TOOLS = frozenset(
    {"checks.run", "checks.start", "dev.command", "dev.command_start", "tests.run"}
)
_JOB_LOOKUPS = frozenset(
    {
        "checks.status", "checks.logs", "checks.cancel",
        "dev.command_status", "dev.command_logs", "dev.command_cancel",
    }
)
#: Browser actions whose free text is user data (a typed value), not a target.
_BROWSER_PRIVATE_TEXT = frozenset({"type", "fill", "press", "insert_text"})


def _safe(value: Any) -> str:
    return str(redact(str(value)))


def _clip(value: str, limit: int = DETAIL_LIMIT) -> str:
    flat = " ".join(str(value).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def argv_text(argv: Any) -> str:
    """An argv as one readable line: the program's base name, then its words.

    The interpreter of a check is usually an absolute path to a virtualenv;
    its directory is noise in a one-line target, its name is not.
    """

    if isinstance(argv, str):
        return argv
    if not isinstance(argv, (list, tuple)) or not argv:
        return ""
    words = [str(item) for item in argv]
    head = words[0].replace("\\", "/")
    if "/" in head:
        words[0] = head.rsplit("/", 1)[-1] or words[0]
    return " ".join(
        f'"{word}"' if (not word or any(ch.isspace() for ch in word)) else word
        for word in words
    )


def _display_url(value: Any) -> str:
    raw = _text(value).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.scheme or not parts.netloc:
        return raw
    # The query string is where tokens travel; the path says what was fetched.
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _quoted(value: Any) -> str:
    text = _text(value)
    return f'"{text}"' if text else ""


def _strip_diff_path(value: str) -> str:
    path = value.split("\t", 1)[0].strip()
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def patch_files(patch: Any) -> list[str]:
    """The files a unified diff touches, in order, without duplicates."""

    files: list[str] = []
    old = ""
    for line in _text(patch).splitlines():
        if line.startswith("--- "):
            old = _strip_diff_path(line[4:])
        elif line.startswith("+++ "):
            new = _strip_diff_path(line[4:])
            name = old if new == "/dev/null" else new
            if name and name != "/dev/null" and name not in files:
                files.append(name)
    return files


def diff_counts(diff: Any) -> tuple[int, int]:
    """Added and removed lines of a unified diff, counted inside hunks only.

    Counting by the hunk header's line totals keeps a removed line that itself
    starts with ``--`` from being mistaken for a file header.
    """

    added = removed = 0
    lines = _text(diff).splitlines()
    index = 0
    while index < len(lines):
        match = _HUNK.match(lines[index])
        index += 1
        if not match:
            continue
        old_left = int(match.group(2) if match.group(2) is not None else 1)
        new_left = int(match.group(4) if match.group(4) is not None else 1)
        while index < len(lines) and (old_left > 0 or new_left > 0):
            line = lines[index]
            index += 1
            if line.startswith("\\"):
                continue
            tag = line[:1]
            if tag == "+":
                added += 1
                new_left -= 1
            elif tag == "-":
                removed += 1
                old_left -= 1
            else:
                old_left -= 1
                new_left -= 1
    return added, removed


def _hunks_only(diff: str) -> list[str]:
    """The hunk lines of a diff (headers dropped), for a bounded preview."""

    rows: list[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if _HUNK.match(line):
            in_hunk = True
            rows.append(line)
            continue
        if line.startswith(("diff --git", "index ")):
            in_hunk = False
            continue
        if not in_hunk and line.startswith(("--- ", "+++ ")):
            continue
        if in_hunk:
            rows.append(line)
    return rows


def _scalar_arguments(arguments: Mapping[str, Any], limit: int = DETAIL_LIMIT) -> str:
    safe = redact(dict(arguments))
    parts: list[str] = []
    for key, value in (safe.items() if isinstance(safe, dict) else ()):
        if isinstance(value, bool) or value is None:
            shown = json.dumps(value)
        elif isinstance(value, (int, float)):
            shown = str(value)
        elif isinstance(value, str):
            shown = json.dumps(_clip(value, 60), ensure_ascii=False)
        elif isinstance(value, (list, tuple)):
            shown = f"[{len(value)}]"
        elif isinstance(value, dict):
            shown = "{…}"
        else:
            continue
        parts.append(f"{key}: {shown}")
    return _clip(", ".join(parts), limit)


def call_detail(core_name: str, arguments: Mapping[str, Any] | None) -> str:
    """The target of one call, for ``Tool(target)``; empty when there is none.

    ``plan.update`` is the one multi-line detail: one ``status<TAB>text`` row
    per item, so a screen can draw the checklist the agent just wrote.
    """

    args: Mapping[str, Any] = arguments if isinstance(arguments, Mapping) else {}
    try:
        detail = _call_detail(core_name, args)
    except Exception:  # noqa: BLE001 - a display helper never fails a tool call
        detail = ""
    if core_name == "plan.update":
        return _safe(detail)[:PREVIEW_LIMIT]
    return _clip(_safe(detail))


def _call_detail(name: str, args: Mapping[str, Any]) -> str:
    path = _text(args.get("path"))
    if name == "repo.read_file":
        return path
    if name == "repo.read_lines":
        start = _int(args.get("start"))
        count = _int(args.get("count"))
        if start and count:
            return f"{path}:{start}-{start + count - 1}"
        if start:
            return f"{path}:{start}"
        return path
    if name == "repo.list_files":
        return _text(args.get("pattern")) or "."
    if name == "repo.search":
        parts = [_quoted(args.get("query"))]
        if _text(args.get("pattern")):
            parts.append(_text(args.get("pattern")))
        if path:
            parts.append(path)
        return ", ".join(part for part in parts if part)
    if name == "repo.inspect":
        return _clip(_text(args.get("goal")), 100)
    if name == "repo.edit_file":
        edits = args.get("edits")
        if isinstance(edits, list) and len(edits) > 1:
            return f"{path} · {len(edits)} edits"
        return path
    if name == "repo.write_file":
        return f"{path} (append)" if args.get("mode") == "append" else path
    if name == "repo.apply_patch":
        return ", ".join(patch_files(args.get("patch")))
    if name == "repo.command":
        payload = args.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if args.get("action") == "apply_patch":
            return "patch " + ", ".join(patch_files(payload.get("patch")))
        operations = payload.get("operations")
        if isinstance(operations, list):
            rows = []
            for item in operations[:4]:
                if not isinstance(item, Mapping):
                    continue
                op = _text(item.get("op"))
                target = _text(item.get("path")) or (
                    f"{_text(item.get('source'))} → {_text(item.get('destination'))}"
                )
                rows.append(f"{op} {target}".strip())
            if len(operations) > 4:
                rows.append(f"+{len(operations) - 4}")
            return ", ".join(rows)
        return _text(args.get("action"))
    if name in _COMMAND_TOOLS and name != "tests.run":
        return argv_text(args.get("argv"))
    if name == "tests.run":
        targets = args.get("targets")
        suite = _text(args.get("suite"))
        if isinstance(targets, list) and targets:
            shown = " ".join(str(item) for item in targets[:4])
            if len(targets) > 4:
                shown += f" +{len(targets) - 4}"
            return shown
        return suite
    if name in _JOB_LOOKUPS:
        return _text(args.get("job_id"))
    if name.startswith("git."):
        verb = name.split(".", 1)[1]
        if verb == "diff" and args.get("staged"):
            return "diff --staged"
        if verb == "commit":
            return f"commit {_quoted(_clip(_text(args.get('message')), 72))}"
        if verb == "push":
            return f"push {_text(args.get('remote'))} {_text(args.get('branch'))}".strip()
        return verb
    if name == "lsp.diagnostics":
        return path
    if name == "plan.update":
        items = args.get("items")
        rows = []
        for item in items[:PLAN_ITEMS] if isinstance(items, list) else []:
            if isinstance(item, Mapping):
                status = _text(item.get("status")) or "todo"
                text = _clip(_text(item.get("text") or item.get("title") or item.get("step")), 120)
            else:
                status, text = "todo", _clip(str(item), 120)
            if text:
                rows.append(f"{status}\t{text}")
        return "\n".join(rows)
    if name == "web.fetch":
        return _display_url(args.get("url"))
    if name == "web.search":
        return _quoted(args.get("query"))
    if name == "browser.command":
        action = _text(args.get("action"))
        target = (
            _text(args.get("page"))
            or _display_url(args.get("url"))
            or _text(args.get("selector"))
        )
        if action not in _BROWSER_PRIVATE_TEXT and not target:
            target = _clip(_text(args.get("text")), 40)
        return f"{action} {target}".strip()
    if name == "visual.check":
        return path
    if name.startswith("skill."):
        skill = _text(args.get("name"))
        extra = _text(args.get("path")) or _text(args.get("script"))
        return f"{skill} {extra}".strip()
    if name == "disk.plan_cleanup":
        paths = args.get("paths")
        return f"{len(paths)} paths" if isinstance(paths, list) else ""
    if name in {"git.status", "runtime.status", "skill.list", "disk.scan"}:
        return ""
    return _scalar_arguments(args)


def result_summary(
    core_name: str, data: Mapping[str, Any] | None, ok: bool, arguments: Mapping[str, Any] | None = None
) -> str:
    """The comma-separated facts of one result: ``path, lines=40``, ``exit 1``.

    Stable ``key=value`` words so a screen can phrase them in its own
    language; a terminal reading the plain stream gets the same words.
    """

    values: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
    args: Mapping[str, Any] = arguments if isinstance(arguments, Mapping) else {}
    try:
        parts = _summary_parts(core_name, values, args)
    except Exception:  # noqa: BLE001 - display only
        parts = []
    if not parts:
        parts.append("ok" if ok else "failed")
    return _safe(", ".join(parts))


def _summary_parts(name: str, data: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    parts: list[str] = []
    exit_code = _int(data.get("exit_code"))
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    if data.get("timed_out") is True:
        parts.append("timed out")
    path = data.get("path")
    if isinstance(path, str) and path:
        parts.append(path)
    if name == "repo.read_file":
        content = data.get("content")
        if isinstance(content, str):
            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            parts.append(f"lines={lines}")
        if data.get("truncated") is True:
            parts.append("truncated")
        return parts
    if name == "repo.read_lines":
        start = _int(data.get("start"))
        lines = data.get("lines")
        total = _int(data.get("total_lines"))
        if start is not None and isinstance(lines, list):
            end = start + max(0, len(lines) - 1)
            parts.append(f"lines={start}-{end}/{total if total is not None else '?'}")
        return parts
    if name == "repo.search":
        count = _int(data.get("match_count"))
        matches = data.get("matches")
        if count is None and isinstance(matches, list):
            count = len(matches)
        if count is not None:
            parts.append(f"matches={count}")
        if isinstance(matches, list):
            files = {
                item.get("path") for item in matches if isinstance(item, Mapping) and item.get("path")
            }
            if files:
                parts.append(f"files={len(files)}")
        if data.get("truncated") is True:
            parts.append("truncated")
        return parts
    if name == "repo.edit_file":
        added, removed = diff_counts(data.get("diff"))
        if added or removed:
            parts.append(f"+{added} -{removed}")
        return parts
    if name == "repo.write_file":
        if args.get("mode") == "append" or data.get("mode") == "append":
            parts.append("appended")
        elif data.get("previous_sha256"):
            parts.append("overwritten")
        else:
            parts.append("created")
        content = args.get("content")
        if isinstance(content, str):
            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            parts.append(f"lines={lines}")
        return parts
    if name in {"repo.apply_patch", "repo.command"}:
        changed = data.get("changed_files")
        if isinstance(changed, list):
            parts.append(f"files={len(changed)}")
        patch = args.get("patch")
        payload = args.get("payload")
        if not patch and isinstance(payload, Mapping):
            patch = payload.get("patch")
        added, removed = diff_counts(patch)
        if added or removed:
            parts.append(f"+{added} -{removed}")
        count = _int(data.get("operation_count"))
        if count is not None and not patch:
            parts.append(f"ops={count}")
        return parts
    if name == "plan.update":
        items = _int(data.get("items"))
        done = _int(data.get("done"))
        if items is not None:
            parts.append(f"items={items}")
        if done is not None:
            parts.append(f"done={done}")
        return parts
    if name in {"checks.start", "dev.command_start"} or name in _JOB_LOOKUPS:
        job = _text(data.get("job_id"))
        status = _text(data.get("status"))
        if job:
            parts.append(f"job={job}")
        if status:
            parts.append(f"status={status}")
        return parts
    if name.startswith("mcp."):
        size = _int(data.get("bytes"))
        if size is not None:
            parts.append(f"bytes={size}")
        return parts
    for key in ("matches", "files", "entries"):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            parts.append(f"{key}={len(value)}")
    return parts


def _bounded(lines: Iterable[str]) -> str:
    rows: list[str] = []
    for line in lines:
        if len(rows) >= PREVIEW_LINES:
            break
        rows.append(line if len(line) <= PREVIEW_LINE_LIMIT else line[: PREVIEW_LINE_LIMIT - 1] + "…")
    return _safe("\n".join(rows))[:PREVIEW_LIMIT]


def _tail(text: str, count: int = 4) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-count:]


def _mcp_text(data: Mapping[str, Any]) -> str:
    result = data.get("result")
    if not isinstance(result, Mapping):
        return ""
    content = result.get("content")
    pieces: list[str] = []
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                pieces.append(_text(item.get("text")))
    if not pieces and "structuredContent" in result:
        try:
            pieces.append(json.dumps(result.get("structuredContent"), ensure_ascii=False)[:600])
        except (TypeError, ValueError):
            pass
    return "\n".join(piece for piece in pieces if piece)


def result_preview(
    core_name: str, data: Mapping[str, Any] | None, ok: bool, arguments: Mapping[str, Any] | None = None
) -> str:
    """A few lines that show what changed or what a command said.

    An edit or patch previews its diff hunks; a command, check or test run
    previews the last lines it printed (stderr when it failed and said
    something there); an MCP call previews its text content. Everything
    else previews nothing -- the summary already says it.
    """

    values: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
    args: Mapping[str, Any] = arguments if isinstance(arguments, Mapping) else {}
    try:
        return _preview(core_name, values, ok, args)
    except Exception:  # noqa: BLE001 - display only
        return ""


def _preview(name: str, data: Mapping[str, Any], ok: bool, args: Mapping[str, Any]) -> str:
    if name == "repo.edit_file":
        diff = _text(data.get("diff"))
        return _bounded(_hunks_only(diff)) if diff else ""
    if name in {"repo.apply_patch", "repo.command"}:
        patch = args.get("patch")
        payload = args.get("payload")
        if not patch and isinstance(payload, Mapping):
            patch = payload.get("patch")
        return _bounded(_hunks_only(_text(patch))) if patch else ""
    if name in {"checks.run", "dev.command", "tests.run"}:
        stdout = _text(data.get("stdout"))
        stderr = _text(data.get("stderr"))
        exit_code = _int(data.get("exit_code"))
        failed = (exit_code not in (None, 0)) or not ok
        lines = _tail(stdout)
        if failed and stderr.strip():
            lines = (lines[-2:] if lines else []) + _tail(stderr, 3)
        elif not lines:
            lines = _tail(stderr, 3)
        return _bounded(lines)
    if name.startswith("mcp."):
        text = _mcp_text(data)
        return _bounded(text.splitlines()[:PREVIEW_LINES]) if text else ""
    return ""


__all__ = [
    "argv_text",
    "call_detail",
    "diff_counts",
    "patch_files",
    "result_preview",
    "result_summary",
]
