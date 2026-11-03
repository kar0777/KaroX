"""How one tool call reads on screen: ``Read(src/app.py)`` and its ``⎿`` line.

Pure functions, so every phrase is testable without a screen. The runtime sends
the canonical tool name, a short redacted target (``detail`` on
``tool_started``), stable ``key=value`` result facts (``summary``) and an
optional preview -- diff hunks or an output tail (``detail`` on
``tool_finished``). This module turns them into the transcript's words in the
reader's language. A runtime that sends none of the new fields still renders:
the title stands alone and the raw summary is the result line.
"""

from __future__ import annotations

import re

from .i18n import plural, tr

#: Canonical tool -> the short title a coding-agent CLI reader already knows.
_TITLES = {
    "repo.read_file": "Read",
    "repo.read_lines": "Read",
    "repo.list_files": "List",
    "repo.tree": "List",
    "repo.search": "Search",
    "repo.inspect": "Inspect",
    "repo.edit_file": "Update",
    "repo.write_file": "Write",
    "repo.apply_patch": "Patch",
    "repo.command": "Files",
    "checks.run": "Check",
    "checks.start": "Check",
    "tests.run": "Tests",
    "dev.command": "Run",
    "dev.command_start": "Run",
    "checks.status": "Job",
    "checks.logs": "Job",
    "checks.cancel": "Job",
    "dev.command_status": "Job",
    "dev.command_logs": "Job",
    "dev.command_cancel": "Job",
    "lsp.diagnostics": "Diagnostics",
    "plan.update": "Plan",
    "web.fetch": "Fetch",
    "web.search": "WebSearch",
    "browser.command": "Browser",
    "visual.check": "Visual",
    "disk.scan": "Disk",
    "disk.plan_cleanup": "Cleanup",
    "runtime.status": "Runtime",
}

#: Canonical tool -> the verb key of the working line ("Reading src/app.py…").
_VERBS = {
    "repo.read_file": "read",
    "repo.read_lines": "read",
    "repo.list_files": "list",
    "repo.tree": "list",
    "repo.search": "search",
    "repo.inspect": "inspect",
    "repo.edit_file": "edit",
    "repo.write_file": "write",
    "repo.apply_patch": "edit",
    "repo.command": "edit",
    "checks.run": "check",
    "checks.start": "check",
    "tests.run": "test",
    "dev.command": "run",
    "dev.command_start": "run",
    "plan.update": "plan",
    "web.fetch": "web",
    "web.search": "web",
    "browser.command": "browser",
    "visual.check": "visual",
}

_EXIT = re.compile(r"^exit (-?\d+)$")


def tool_title(tool: str) -> str:
    """``Read``, ``Update``, ``Run`` … or ``github · list_issues (MCP)``."""

    if not tool:
        return "Tool"
    if tool.startswith("mcp."):
        _, _, rest = tool.partition(".")
        namespace, _, remote = rest.partition(".")
        return f"{namespace} · {remote or namespace} (MCP)"
    if tool in _TITLES:
        return _TITLES[tool]
    if tool.startswith("git."):
        return "Git"
    if tool.startswith("skill."):
        return "Skill"
    if tool.startswith(("agent.", "task.", "subagent.")):
        return "Task"
    if tool.startswith(("desktop.", "computer.")):
        return "Desktop"
    return tool


def call_line(tool: str, detail: str = "") -> str:
    """The head of a tool line: ``Title(target)``; a plan's rows are not a target."""

    title = tool_title(tool)
    target = "" if "\t" in detail else " ".join(detail.split())
    if tool.startswith("git.") and target:
        return f"{title}({target})"
    return f"{title}({target})" if target else title


def working_text(tool: str, detail: str, lang: str) -> str:
    """The working line's phrase for a running call, in the reader's language."""

    target = "" if "\t" in detail else " ".join(detail.split())
    if len(target) > 70:
        target = target[:69] + "…"
    if tool.startswith("mcp."):
        return tr(lang, "verb.mcp", target=tool_title(tool).replace(" (MCP)", ""))
    if tool.startswith("git."):
        return tr(lang, "verb.git", target=target or tool.split(".", 1)[1])
    if tool.startswith("skill."):
        return tr(lang, "verb.skill", target=target or tool.split(".", 1)[1])
    verb = _VERBS.get(tool)
    if verb is None:
        return tr(lang, "verb.other", target=call_line(tool, detail))
    if not target:
        return tr(lang, f"verb.{verb}.bare")
    return tr(lang, f"verb.{verb}", target=target)


def summary_facts(summary: str) -> dict[str, str]:
    """``path, lines=40, exit 0`` -> ``{"path": …, "lines": "40", "exit": "0"}``."""

    facts: dict[str, str] = {}
    for raw in summary.split(","):
        part = raw.strip()
        if not part:
            continue
        exit_match = _EXIT.match(part)
        if exit_match:
            facts["exit"] = exit_match.group(1)
            continue
        if part.startswith("+") and " -" in part:
            added, _, removed = part.partition(" -")
            if added[1:].isdigit() and removed.isdigit():
                facts["added"], facts["removed"] = added[1:], removed
                continue
        key, sep, value = part.partition("=")
        if sep and key.isidentifier():
            facts[key] = value
        elif part in {"created", "overwritten", "appended", "truncated", "timed out", "ok", "failed"}:
            facts[part.replace(" ", "_")] = "1"
        elif part.startswith("reused:"):
            facts["reused"] = part.split("step", 1)[-1].strip()
        elif "path" not in facts:
            facts["path"] = part
    return facts


def result_line(tool: str, ok: bool | None, summary: str, lang: str) -> str:
    """The ``⎿`` line: what the call found or did, never the raw ``k=v`` words."""

    if ok is None:
        return tr(lang, "toolres.running")
    facts = summary_facts(summary)
    if "reused" in facts:
        return tr(lang, "toolres.reused", step=facts["reused"])
    if tool in {"repo.read_file"} and "lines" in facts and facts["lines"].isdigit():
        line = tr(lang, "toolres.read", lines=plural(lang, int(facts["lines"]), "line"))
        return line + (tr(lang, "toolres.truncated") if "truncated" in facts else "")
    if tool == "repo.read_lines" and "lines" in facts:
        span, _, total = facts["lines"].partition("/")
        start, _, end = span.partition("-")
        return tr(lang, "toolres.read_range", start=start, end=end or start, total=total or "?")
    if tool == "repo.search" and "matches" in facts and facts["matches"].isdigit():
        count = int(facts["matches"])
        if not count:
            return tr(lang, "toolres.no_matches")
        files = facts.get("files", "")
        if files.isdigit():
            return tr(
                lang,
                "toolres.found_in",
                matches=plural(lang, count, "match"),
                files=plural(lang, int(files), "file"),
            )
        return tr(lang, "toolres.found", matches=plural(lang, count, "match"))
    if tool in {"repo.list_files", "repo.tree"} and "files" in facts and facts["files"].isdigit():
        return tr(lang, "toolres.listed", files=plural(lang, int(facts["files"]), "file"))
    if tool == "repo.edit_file" and "added" in facts and ok:
        return tr(lang, "toolres.edited", added=facts["added"], removed=facts["removed"])
    if tool == "repo.write_file" and ok:
        lines = facts.get("lines", "")
        counted = plural(lang, int(lines), "line") if lines.isdigit() else ""
        for word in ("appended", "overwritten", "created"):
            if word in facts:
                return tr(lang, f"toolres.{word}", lines=counted).rstrip(" ·")
    if tool in {"repo.apply_patch", "repo.command"} and ok and "files" in facts:
        files = facts["files"]
        counted = plural(lang, int(files), "file") if files.isdigit() else files
        if "added" in facts:
            return tr(
                lang, "toolres.patched", files=counted,
                added=facts["added"], removed=facts["removed"],
            )
        return tr(lang, "toolres.files_changed", files=counted)
    if tool == "plan.update" and "items" in facts:
        items = facts["items"]
        counted = plural(lang, int(items), "item") if items.isdigit() else items
        return tr(lang, "toolres.plan", items=counted, done=facts.get("done", "0"))
    if tool in {"checks.start", "dev.command_start"} and "job" in facts:
        return tr(lang, "toolres.job_started", job=facts["job"])
    if "job" in facts or "status" in facts:
        status = facts.get("status", "")
        text = tr(lang, "toolres.job_status", status=status or "?")
        if "exit" in facts:
            text += " · " + tr(lang, "toolres.exit", code=facts["exit"])
        return text
    if "exit" in facts:
        code = facts["exit"]
        if "timed_out" in facts:
            return tr(lang, "toolres.timed_out")
        if tool == "tests.run":
            return tr(lang, "toolres.tests_ok" if code == "0" else "toolres.tests_failed", code=code)
        if tool.startswith("checks."):
            return tr(lang, "toolres.check_ok" if code == "0" else "toolres.check_failed", code=code)
        return tr(lang, "toolres.exit", code=code)
    if tool.startswith("mcp.") and ok:
        return tr(lang, "toolres.done")
    if not ok:
        detail = summary.strip()
        return tr(lang, "toolres.error", detail=detail) if detail and detail != "failed" else tr(
            lang, "toolres.failed"
        )
    cleaned = summary.strip()
    if not cleaned or cleaned == "ok":
        return tr(lang, "toolres.done")
    return cleaned


def plan_rows(detail: str) -> list[tuple[str, str]]:
    """A plan call's ``status<TAB>text`` rows, in order."""

    rows: list[tuple[str, str]] = []
    for line in detail.splitlines():
        status, sep, text = line.partition("\t")
        if sep and text.strip():
            rows.append((status.strip() or "todo", text.strip()))
    return rows


def next_plan_item(rows: list[tuple[str, str]]) -> str:
    """The item the agent is on: the first ``doing``, else the first ``todo``."""

    return plan_step(rows)[2]


def plan_step(rows: list[tuple[str, str]]) -> tuple[int, int, str]:
    """``(position, total, text)`` of the item the agent is on; ``(0, total, "")`` when done."""

    for wanted in ("doing", "todo"):
        for index, (status, text) in enumerate(rows, start=1):
            if status == wanted:
                return index, len(rows), text
    return 0, len(rows), ""


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_rows(preview: str) -> list[tuple[str, int, str]]:
    """Hunk lines as ``(kind, line_number, text)``; kind is ``+``, ``-``, `` `` or ``…``.

    Numbers follow the new file for added and context lines and the old file
    for removed ones, the way a side-by-side reader expects. A preview cut in
    the middle of a hunk simply ends.
    """

    rows: list[tuple[str, int, str]] = []
    old_no = new_no = 0
    seen_hunk = False
    for line in preview.splitlines():
        match = _HUNK.match(line)
        if match:
            if seen_hunk:
                rows.append(("…", 0, ""))
            seen_hunk = True
            old_no, new_no = int(match.group(1)), int(match.group(3))
            continue
        if not seen_hunk or line.startswith("\\"):
            continue
        tag, text = line[:1], line[1:]
        if tag == "+":
            rows.append(("+", new_no, text))
            new_no += 1
        elif tag == "-":
            rows.append(("-", old_no, text))
            old_no += 1
        else:
            rows.append((" ", new_no, text))
            old_no += 1
            new_no += 1
    return rows


def looks_like_diff(preview: str) -> bool:
    return any(_HUNK.match(line) for line in preview.splitlines()[:3])


def duration_text(seconds: float, lang: str) -> str:
    """``4 с`` / ``1 мин 5 с`` -- whole seconds, the way a reader counts."""

    total = max(0, int(round(seconds)))
    if total < 60:
        return tr(lang, "time.seconds", s=total)
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return tr(lang, "time.minutes", m=minutes, s=rest)
    hours, minutes = divmod(minutes, 60)
    return tr(lang, "time.hours", h=hours, m=minutes)


def short_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(max(0, value))


__all__ = [
    "call_line",
    "diff_rows",
    "duration_text",
    "looks_like_diff",
    "next_plan_item",
    "plan_step",
    "plan_rows",
    "result_line",
    "short_tokens",
    "summary_facts",
    "tool_title",
    "working_text",
]

