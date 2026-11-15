"""Observation/result-body rendering for the engine tool environment.

These are pure translations from Core results to the engine's observation
strings: fence salvage, layout/refusal notices, compact diffs, mutation and
check bodies. The CoreToolEnvironment class imports them; tests also import
several names through engine_tool_env for compatibility.
"""

from __future__ import annotations

import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from . import check_baseline
from .check_report import render_failure_map
from .unified_patch import codex_patch_files
from .test_output import (  # noqa: E402
    CONDENSED_MAX_CHARS_WITH_MAP,
    condense_test_output,
)


_FENCE_LANGUAGES = {
    ".html": "html", ".htm": "html", ".svg": "svg", ".xml": "xml", ".css": "css",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "tsx", ".py": "python", ".json": "json",
    ".md": "markdown", ".sh": "bash", ".rs": "rust", ".go": "go", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".rb": "ruby",
    ".php": "php", ".sql": "sql", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".kt": "kotlin", ".swift": "swift", ".cs": "csharp", ".lua": "lua",
}

#: Process bookkeeping that differs between two otherwise identical runs of
#: the same call. The model still sees it; the no-progress check compares
#: results without it, or a repeated `git diff` would never look repeated.
_VOLATILE_RESULT_KEYS = frozenset({"bridge_pid", "child_pid", "duration_ms", "elapsed_ms"})

#: What resets the research clock. Downloading an upstream reference file is
#: still research, not progress: in the fix-only run the model saved a dozen
#: `_tmp_*` copies and every save reset the budget, so the notice never fired
#: and the first real edit waited until step 26.
_CODE_CHANGE_TOOLS = frozenset(
    {"repo.edit_file", "repo.write_file", "repo.apply_patch", "repo.command"}
)


def fenced_file(relative: str, text: str, *, label: bool = False) -> str:
    """One file as a Markdown code block whose fence cannot occur inside it."""

    longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=0)
    fence = "`" * max(3, longest + 1)
    language = _FENCE_LANGUAGES.get(Path(relative).suffix.lower(), "")
    body = text if text.endswith("\n") else text + "\n"
    block = f"{fence}{language}\n{body}{fence}"
    return f"`{relative}`\n{block}" if label else block

def layout_problem(
    page: str, width: int, height: int, measured: Optional[Mapping[str, Any]]
) -> str:
    """One line for a page that is wider than its viewport, else ""."""

    if not isinstance(measured, Mapping):
        return ""
    value = measured.get("value")
    if not isinstance(value, Mapping):
        return ""
    try:
        scroll_width = int(value.get("scroll_width") or 0)
        viewport_width = int(value.get("viewport_width") or width)
    except (TypeError, ValueError):
        return ""
    if scroll_width <= viewport_width + 1:
        return ""
    line = (
        f"{page} at {width}x{height}: the document is {scroll_width} px wide in a "
        f"{viewport_width} px viewport, so the page scrolls sideways"
    )
    widest = value.get("widest")
    if isinstance(widest, Mapping) and widest.get("element"):
        line += (
            f"; furthest right: {widest.get('element')} "
            f"(x {widest.get('left')}-{widest.get('right')} px, {widest.get('width')} px wide)"
        )
    return line

def layout_refusal_message(problems: List[str]) -> str:
    lines = "\n".join(f"- {item}" for item in problems)
    return (
        "finish was not accepted: the repository changed after these viewports were "
        "last checked, and KaroX opened the page again at each of them now:\n"
        f"{lines}\n"
        "Fix the layout, check that size once more, then finish. This re-check did "
        "not use the browser budget; the browser tab is left at the last size above."
    )

def _counts_as_repository_change(
    core_name: str, data: Mapping[str, Any], ok: bool
) -> bool:
    return (
        ok
        and core_name in _CODE_CHANGE_TOOLS
        and (data.get("changed") is True or bool(data.get("changed_files")))
    )

_REPO_DELETE_ACTIONS = frozenset({"delete", "remove", "rm", "unlink", "delete_file", "delete_files", "remove_file"})

def _patch_header_path(value: str) -> Optional[str]:
    value = value.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value.replace("\\", "/") or None

def _pure_delete_targets(arguments: Mapping[str, Any]) -> List[str]:
    """Files a patch only deletes: every file goes to ``/dev/null``, nothing added."""

    patch = arguments.get("patch")
    if not isinstance(patch, str):
        payload = arguments.get("payload")
        patch = payload.get("patch") if isinstance(payload, Mapping) else None
    if not isinstance(patch, str) or not patch.strip():
        return []
    sections = codex_patch_files(patch)
    if sections:
        if any(kind != "delete" for kind, _path in sections):
            return []
        return list(dict.fromkeys(path.replace("\\", "/") for _kind, path in sections))
    lines = patch.splitlines()
    paths: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old = _patch_header_path(line[4:])
            new = _patch_header_path(lines[index + 1][4:])
            if old is None or new is not None:
                return []
            paths.append(old)
            index += 2
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            return []
        index += 1
    return list(dict.fromkeys(paths))

def scratch_delete_paths(arguments: Mapping[str, Any], created: Any) -> List[str]:
    """Files a pure-deletion patch removes, when this session created all of them.

    A unified diff whose every file goes to ``/dev/null`` only says "delete
    these files", yet Core's strict parser wants the body to repeat each file
    exactly. A measured 3D run (2026-11-04) tried to remove its own probe.html
    that way three times: two guessed bodies were refused, apply_patch was
    disabled, and the run left a placeholder page next to the deliverable. For
    a file this session wrote, the body tells the session nothing it does not
    know, so such a patch is the delete operation the prompt already names.
    Anything else -- a patch that also edits, renames or creates, or touches a
    file the session did not create -- returns no paths and goes to Core as
    sent. Core still decides whether the delete may run.
    """

    owned = {str(item).replace("\\", "/") for item in (created or ())}
    if not owned:
        return []
    paths = _pure_delete_targets(arguments)
    if not paths or any(path not in owned for path in paths):
        return []
    return paths

def absent_delete_paths(arguments: Mapping[str, Any], repository: Any) -> List[str]:
    """Targets of a pure-deletion patch when none of them exists any more.

    Deleting a file that is already gone changes nothing, but the destructive
    gate saw only "delete a file this session does not own" -- the session
    forgets a file once it is deleted -- and refused it. A measured run
    (eng-task-queue, 2026-11-05) re-sent its delete after an earlier one had
    already removed the probe, read the refusal as "deletion is blocked", and
    wrote the probe back as a "manual smoke script" that shipped in the diff.
    """

    paths = _pure_delete_targets(arguments)
    if not paths or repository is None:
        return []
    try:
        root = Path(str(repository)).resolve()
    except OSError:
        return []
    for path in paths:
        candidate = root / path
        try:
            candidate.resolve().relative_to(root)
        except (OSError, ValueError):
            return []
        if candidate.exists() or candidate.is_symlink():
            return []
    return paths

def _requested_dry_run(arguments: Mapping[str, Any]) -> bool:
    if arguments.get("dry_run") is True:
        return True
    payload = arguments.get("payload")
    return isinstance(payload, Mapping) and payload.get("dry_run") is True

_EDIT_ALIASES = (("old", "old_string"), ("new", "new_string"))

def _normalize_edit_file(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Top-level old/new mean old_string/new_string when those are absent."""

    normalized = dict(arguments)
    if "edits" in normalized:
        return normalized
    for alias, canonical in _EDIT_ALIASES:
        if alias in normalized and canonical not in normalized:
            normalized[canonical] = normalized.pop(alias)
    return normalized

#: Names other tool APIs give the same argument (str_replace_editor's
#: old_str/new_str, a shell tool's command, a reader's offset). Each was refused
#: as an unknown argument and cost a step; any model trained on that API sends
#: them again.
_EDIT_STRING_ALIASES = (
    ("old_string", ("old_str", "old_text", "search", "find", "original")),
    ("new_string", ("new_str", "new_text", "replace", "replacement", "updated")),
)
_CHECK_ARGV_ALIASES = ("command", "cmd", "args")
_DIFF_PATH_ALIASES = ("path", "file", "files")
#: A command string with shell syntax has no faithful argv; it stays a string
#: so Core refuses it instead of running "cd" or "&&" as a program.
_SHELL_SYNTAX = re.compile(r"[|&;<>()$`\n]")


def normalize_tool_arguments(core_name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Rename an argument another tool API spells differently; nothing else.

    Only a spelling with one reading is renamed, and only when the canonical
    argument is absent. Core validates the result as if the model sent it.
    """

    args = dict(arguments)
    if core_name == "repo.edit_file" and "edits" not in args:
        args = _normalize_edit_file(args)
        for canonical, aliases in _EDIT_STRING_ALIASES:
            if canonical in args:
                continue
            for alias in aliases:
                if isinstance(args.get(alias), str):
                    args[canonical] = args.pop(alias)
                    break
    elif core_name == "checks.run":
        if "argv" not in args:
            for alias in _CHECK_ARGV_ALIASES:
                if isinstance(args.get(alias), (str, list)):
                    args["argv"] = args.pop(alias)
                    break
        argv = args.get("argv")
        if isinstance(argv, str) and argv.strip() and not _SHELL_SYNTAX.search(argv):
            try:
                args["argv"] = shlex.split(argv)
            except ValueError:
                pass
        timeout = args.get("timeout")
        if "timeout_seconds" not in args and isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            args["timeout_seconds"] = args.pop("timeout")
    elif core_name == "repo.read_lines" and "start" not in args and "offset" in args:
        offset = args.pop("offset")
        # A reader's offset is a line to start from; 0 means the first line.
        args["start"] = max(1, offset) if isinstance(offset, int) and not isinstance(offset, bool) else offset
    elif core_name == "git.diff" and "paths" not in args:
        for alias in _DIFF_PATH_ALIASES:
            value = args.get(alias)
            if isinstance(value, str) and value.strip():
                args.pop(alias)
                args["paths"] = [value]
                break
            if isinstance(value, list):
                args["paths"] = args.pop(alias)
                break
    return args


def _normalize_repo_command(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Rewrite unambiguous near-miss repo.command shapes into the schema.

    Only a shape with exactly one reading is rewritten; anything else is passed
    through unchanged so Core refuses it with its usual message. Core still
    validates every operation, so this cannot widen what a call may do.
    """

    result = dict(arguments)
    payload = result.get("payload")
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            decoded = {"operations": decoded}
        if not isinstance(decoded, dict):
            return result
        payload = decoded
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return result
    payload = dict(payload)
    for key in ("operations", "patch"):
        if key in result and key not in payload:
            payload[key] = result.pop(key)
    action = result.get("action")
    name = action.strip().lower() if isinstance(action, str) else ""
    if name in _REPO_DELETE_ACTIONS:
        paths: List[str] = []
        for source in (result, payload):
            for key in ("path", "file"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    paths.append(value)
            for key in ("paths", "files"):
                value = source.get(key)
                if isinstance(value, list):
                    paths.extend(item for item in value if isinstance(item, str) and item.strip())
        if not paths or "operations" in payload or "patch" in payload:
            return dict(arguments)
        unique = list(dict.fromkeys(paths))
        return {
            "action": "batch",
            "payload": {"operations": [{"op": "delete", "path": item} for item in unique]},
        }
    if not name:
        if "operations" in payload and "patch" not in payload:
            name = "batch"
        elif "patch" in payload and "operations" not in payload:
            name = "apply_patch"
        else:
            return dict(arguments)
    if name not in {"batch", "apply_patch"}:
        return dict(arguments)
    extras = set(result) - {"action", "payload"}
    if extras:
        return dict(arguments)
    return {"action": name, "payload": payload}

def _batch_tail(action: Mapping[str, Any] | None) -> bool:
    """True when this action ends a batched response (or carries no batch data)."""

    if not isinstance(action, Mapping):
        return True
    index = action.get("batch_index")
    size = action.get("batch_size")
    if isinstance(index, int) and isinstance(size, int) and not isinstance(size, bool):
        return size <= 1 or index == size - 1
    return True

def _stable_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_result(item)
            for key, item in value.items()
            if key not in _VOLATILE_RESULT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_result(item) for item in value]
    return value

def _progress_key(core_name: str, ok: bool, data: Mapping[str, Any]) -> str:
    """A digest of what a result says, without pids, timings or evidence ids."""

    try:
        text = json.dumps(
            [core_name, bool(ok), _stable_result(dict(data))],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

PYTEST_MISSING_HINT = (
    "pytest is not installed in this project's interpreter, so tests.run "
    "cannot run here. This is not a test failure. Use checks.run with the "
    "approved command (it uses the project's own runner) and do not retry "
    "tests.run."
)

_PYTEST_MISSING = re.compile(r"No module named '?pytest'?(?:\s|$)")

def _pytest_missing(data: Mapping[str, Any]) -> bool:
    for key in ("stderr", "stdout"):
        value = data.get(key)
        if isinstance(value, str) and _PYTEST_MISSING.search(value[-4_000:]):
            return True
    return False

#: Characters of one line shown by repo.read_lines; the durable result keeps the whole line.
READ_LINE_CHARS = 3_000


def _long_line_cut(line: str) -> str:
    """Cut a minified or generated line instead of sending all of it.

    rebench26 v616rb nicegui-5858: reading a two-line bundled ``dist/index.js``
    put 50k characters into the context, re-sent with every later request.
    """

    if len(line) <= READ_LINE_CHARS:
        return line
    rest = len(line) - READ_LINE_CHARS
    return f"{line[:READ_LINE_CHARS]} …[{rest} more characters on this line; repo_search finds text inside it]"


def _read_lines_body(data: Mapping[str, Any]) -> str:
    """The window as plain lines, not a JSON envelope around a list.

    The tool already returns the window start and the total; re-encoding every
    line as a JSON string costs a quote and a comma per line, escapes the
    content, and hides the numbers the header already carries. Hashes and the
    secret flag stay in the durable result.
    """

    path = data.get("path")
    lines = data.get("lines")
    start = data.get("start")
    if not isinstance(lines, list) or not isinstance(start, int) or isinstance(start, bool):
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    total = data.get("total_lines")
    last = start + len(lines) - 1 if lines else start
    header = f"[repo.read_lines {path} {start}-{last}"
    if isinstance(total, int) and not isinstance(total, bool):
        header += f" of {total}"
    header += "]"
    body = "\n".join(_long_line_cut(str(line)) for line in lines)
    text = f"{header}\n{body}" if body else header
    if data.get("has_more"):
        text += "\n…more lines below"
    return text

def _visual_check_body(data: Mapping[str, Any], ok: bool) -> str:
    """The verdict the model acts on, without the task echo.

    The durable result keeps ``completion_identity`` so the completion gate can
    bind the verdict to the bytes; this body keeps only what changes the next
    action: the verdict, the named issues, runtime errors, the resize verdict
    and where the frames are. The whole task text is already in the run's first
    message and does not belong in every later request.
    """

    verdict = str(data.get("verdict") or ("pass" if ok else "failed"))
    method = data.get("method")
    header = f"[visual.check {verdict}"
    if isinstance(method, str) and method:
        header += f" · {method}"
    header += "]"
    lines = [header]
    left = data.get("checks_left")
    if isinstance(left, int) and not isinstance(left, bool):
        lines.append(f"checks left: {left}")
    if verdict == "pass":
        # A pass is the end of the visual loop, not permission to keep
        # polishing. The 3d-viewer run saw a pass, then spent twenty more
        # steps on an unrequested pan-offset fix, invalidated the verdict and
        # hit the wall clock with a page the grader accepted.
        lines.append(
            "This pass covers the current bytes: run the final change review "
            "and finish. Do not edit to address the notes below; an edit "
            "invalidates this pass. Edit again only if you found a defect the "
            "checker could not see."
        )
    elif method == "limit":
        # The budget exists to stop polishing, not to make the model guess at
        # feedback it can no longer get. The aquarium run spent 26 more steps
        # editing after this verdict and broke a working resize.
        lines.append("No checks remain: stop changing the page and finish.")
    elif left == 0:
        lines.append("This was the last check: fix only what it lists, then finish.")
    issues = data.get("issues")
    if isinstance(issues, list) and issues:
        if verdict == "pass":
            lines.append(
                "optional cosmetic notes (do not edit for these; editing "
                "invalidates the pass):"
            )
        else:
            lines.append("issues:")
        lines.extend(f"- {str(item)[:200]}" for item in issues[:5])
    for label, key in (
        ("console errors", "console_errors"),
        ("page errors", "page_errors"),
    ):
        values = data.get(key)
        if isinstance(values, list) and values:
            lines.append(label + ": " + "; ".join(str(item)[:200] for item in values[:3]))
    resize = data.get("resize")
    if isinstance(resize, dict):
        lines.append(f"resize: {'ok' if resize.get('ok') else 'failed'}")
    frames = data.get("frames")
    if isinstance(frames, list) and frames:
        lines.append("frames: " + ", ".join(str(item) for item in frames[:2]))
    return "\n".join(lines)

DIFF_ECHO_MAX_CHARS = 800

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

def _compact_diff(diff: str) -> str:
    """A short diff verbatim; a long one as its size and hunk locations."""

    if len(diff) <= DIFF_ECHO_MAX_CHARS:
        return diff
    added = removed = 0
    starts: List[str] = []
    for line in diff.splitlines():
        match = _HUNK.match(line)
        if match:
            starts.append(match.group(2))
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    shown = ", ".join(starts[:12]) + (", ..." if len(starts) > 12 else "")
    hunks = f"{len(starts)} hunk(s) at new line {shown}" if starts else "no hunk headers"
    return f"diff: +{added} -{removed} lines, {hunks} (applied as sent)"

def _mutation_body(core_name: str, data: Mapping[str, Any], ok: bool) -> str:
    """The small, decision-shaped view of a successful or failed mutation.

    The full result (hashes, previous hashes, secret flags, evidence ids) stays
    in the session record. This body is what the model reads and what every
    later request re-sends, so it keeps only the facts a next edit needs.
    """

    path = data.get("path")
    header = f"[{core_name} {path}]" if isinstance(path, str) and path else f"[{core_name}]"
    lines = [header]
    facts: List[str] = []
    if data.get("changed") is True:
        facts.append("changed")
    replacements = data.get("replacements")
    if isinstance(replacements, int) and not isinstance(replacements, bool) and replacements > 0:
        facts.append(f"{replacements} replacement(s)")
    changed_files = data.get("changed_files")
    if isinstance(changed_files, list) and changed_files:
        shown = [str(item) for item in changed_files[:8]]
        suffix = f" (+{len(changed_files) - 8})" if len(changed_files) > 8 else ""
        facts.append("files: " + ", ".join(shown) + suffix)
    size = data.get("bytes")
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        facts.append(f"{size} bytes")
    if facts:
        lines.append(" · ".join(facts))
    diff = data.get("diff")
    if isinstance(diff, str) and diff.strip():
        lines.append(_compact_diff(diff.strip()))
    for key in ("quick_checks_line", "affected_tests_line"):
        line = data.get(key)
        if isinstance(line, str) and line.strip():
            lines.append(line.strip())
    if not ok:
        error = data.get("error") or data.get("detail") or data.get("message")
        if isinstance(error, dict):
            error = json.dumps(error, ensure_ascii=False, sort_keys=True)
        if isinstance(error, str) and error.strip():
            lines.append("error: " + error.strip()[:2_000])
        elif len(lines) == 1:
            lines.append("failed")
        # A one-line anchor that missed has no closest block in the message;
        # its nearest file lines spare the model a re-read to find them.
        if "closest block" not in str(error or ""):
            similar: List[str] = []
            for failure in data.get("failures") or ():
                if not isinstance(failure, Mapping):
                    continue
                for candidate in failure.get("candidates") or ():
                    if isinstance(candidate, Mapping) and candidate.get("line"):
                        similar.append(f"  line {candidate.get('line')}: {candidate.get('fragment')}")
            if similar:
                lines.append("similar lines in the file:\n" + "\n".join(similar[:6]))
    return "\n".join(lines)

def _plain_body(core_name: str, data: Mapping[str, Any]) -> Optional[str]:
    """Plain text for search/listing/git results: the JSON envelope (pids, hashes, escaped newlines) cost ~2x the tokens."""
    if core_name == "repo.search" and isinstance(data.get("matches"), list):
        head = f"[repo.search {data.get('query')!r}: {data.get('match_count')} match(es) in {data.get('files_scanned')} file(s){', truncated' if data.get('truncated') else ''}]"
        rows = [head]
        for m in data["matches"]:
            if not isinstance(m, dict):
                continue
            rows.append(f"{m.get('path')}:{m.get('line')}: {m.get('text')}{' …' if m.get('clipped') else ''}")
            found = m.get("definition")
            if isinstance(found, dict) and isinstance(found.get("text"), str):
                start = int(found.get("start") or m.get("line") or 1)
                more = "" if found.get("complete") else " (continues; read_lines for the rest)"
                rows.append(f"[definition, lines {start}-{found.get('end')}{more}]")
                rows.extend(f"{start + i:>6}\t{line}" for i, line in enumerate(found["text"].split("\n")))
        return "\n".join(rows)
    if core_name == "repo.list_files" and isinstance(data.get("files"), list) and all(isinstance(f, str) for f in data["files"]):
        return "\n".join([f"[repo.list_files{' truncated' if data.get('truncated') else ''}]"] + data["files"])
    if core_name.startswith("git.") and isinstance(data.get("stdout"), str):
        cut = " · output truncated" if data.get("stdout_truncated") else ""
        err = str(data.get("stderr") or "").strip()
        return f"[{core_name} exit {data.get('exit_code')}{cut}]\n{data['stdout']}" + (f"\n[stderr]\n{err}" if err else "")
    return None

def _checks_body(data: Mapping[str, Any]) -> str:
    """What the model reads for one finished check.

    The failure map comes first: it is the part that survives clipping and it
    names the file and line a repair has to edit. The raw streams follow,
    condensed when long. stderr is appended whenever it spoke, because Jest and
    cargo report there; reading stdout alone made a red run look silent.
    """

    stdout = str(data.get("stdout") or "")
    stderr = str(data.get("stderr") or "")
    if stdout.strip():
        primary, secondary = stdout, stderr
    else:
        primary, secondary = stderr, ""
    text = primary
    rendered = render_failure_map(data.get("failure_map"))
    if rendered:
        # The map already names every failure, file and line. The raw log is
        # corroboration, not the primary signal, so keep a short slice instead
        # of the full condensed log on every later request.
        condensed = condense_test_output(
            text, max_chars=CONDENSED_MAX_CHARS_WITH_MAP, threshold=0
        )
    else:
        condensed = condense_test_output(text)
    if condensed is not None:
        text = str(condensed.get("text") or text)
    if secondary.strip():
        text = f"{text}\n[stderr]\n{secondary}" if text.strip() else secondary
    if rendered:
        text = f"{rendered}\n{text}" if text.strip() else rendered
    counted = check_baseline.quiet_count(data)
    if counted:
        text = f"{text}\n[pytest printed no count line; its progress rows show {counted}]"
    note = "\n".join(p for p in (check_baseline.render(data), str(data.get("detail") or "")) if p)
    if note:
        text = f"{note}\n{text}" if text.strip() else note
    return text

def _failed_check_report(record: Any) -> Optional[Dict[str, Any]]:
    """The parsed failure map of the latest check-family call, if it failed.

    The latest call decides: a green check after a red one means the repair
    already landed, and an old map must not drive another advisory run.
    """

    for entry in reversed(record.provider_history):
        if entry.get("role") != "tool":
            continue
        if entry.get("core_name") not in {"checks.run", "tests.run"}:
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        data = result.get("data")
        if not isinstance(data, dict):
            continue
        passed = (
            result.get("ok") is True
            and data.get("exit_code") == 0
            and not data.get("timed_out")
        ) or data.get("preexisting_only") is True
        if passed:
            return None
        report = data.get("failure_map")
        return report if isinstance(report, dict) and report.get("failures") else None
    return None
