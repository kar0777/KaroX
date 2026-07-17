"""High-leverage repository tools registered on the KaroX Notion MCP server.

Kept separate from the transport gateway so agent capabilities can evolve without
risking authentication or Streamable HTTP middleware regressions.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

CallKaroX = Callable[..., Awaitable[dict[str, Any]]]


def _json_array(value: str, name: str) -> tuple[Optional[list[Any]], Optional[dict[str, Any]]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, {"ok": False, "status": 400, "error": f"Invalid {name} JSON: {exc}"}
    if not isinstance(parsed, list):
        return None, {"ok": False, "status": 400, "error": f"{name} must decode to an array"}
    return parsed, None


def _data(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("data")
    return value if isinstance(value, dict) else {}


def _line_slice(content: str, start_line: int, end_line: int, max_chars: int, line_numbers: bool) -> dict[str, Any]:
    lines = content.splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    end = total if int(end_line or 0) <= 0 else min(total, int(end_line))
    if end < start:
        end = start - 1
    selected = lines[start - 1 : end]
    if line_numbers:
        width = len(str(max(end, 1)))
        text = "\n".join(f"{index:>{width}} | {line}" for index, line in enumerate(selected, start=start))
    else:
        text = "\n".join(selected)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n… [truncated {len(text) - max_chars} chars]"
    return {
        "content": text,
        "startLine": start,
        "endLine": end,
        "totalLines": total,
        "truncated": truncated,
    }


def _query_snippets(content: str, query: str, *, max_snippets: int = 3, radius: int = 220) -> list[str]:
    snippets: list[str] = []
    cursor = 0
    while len(snippets) < max_snippets:
        index = content.find(query, cursor)
        if index < 0:
            break
        start = max(0, index - radius)
        end = min(len(content), index + len(query) + radius)
        snippets.append(("…" if start else "") + content[start:end] + ("…" if end < len(content) else ""))
        cursor = index + max(1, len(query))
    return snippets


def register(mcp: Any, call: CallKaroX) -> None:
    """Register tools once on an existing FastMCP instance."""
    if getattr(mcp, "_karox_agent_tools_registered", False):
        return

    @mcp.tool()
    async def karox_list_dir(path: str = "", max_entries: int = 500) -> dict[str, Any]:
        """List one directory instead of loading the entire repository tree."""
        return await call(
            "GET",
            "/tree/dir",
            params={"path": path, "max_files": max(1, min(int(max_entries), 5000))},
        )

    @mcp.tool()
    async def karox_search(
        query: str,
        glob: str = "*",
        max_files: int = 100,
        include_snippets: bool = True,
    ) -> dict[str, Any]:
        """Search file contents and optionally return small matching snippets."""
        search = await call(
            "GET",
            "/files/search",
            params={"q": query, "glob": glob or "*", "max_files": max(1, min(int(max_files), 500))},
        )
        if not search.get("ok") or not include_snippets:
            return search
        payload = _data(search)
        files = payload.get("files") if isinstance(payload.get("files"), list) else []
        paths = [str(item.get("path")) for item in files[:20] if isinstance(item, dict) and item.get("path")]
        if not paths:
            return search
        read = await call("POST", "/files/read", body={"paths": paths})
        if not read.get("ok"):
            return {"ok": True, "status": search.get("status"), "data": {**payload, "snippetError": read}}
        by_path: dict[str, str] = {}
        for item in _data(read).get("files", []):
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                by_path[str(item.get("path"))] = item["content"]
        enriched = []
        for item in files:
            if not isinstance(item, dict):
                continue
            current = dict(item)
            content = by_path.get(str(item.get("path")), "")
            current["snippets"] = _query_snippets(content, query) if content else []
            enriched.append(current)
        return {"ok": True, "status": search.get("status"), "data": {**payload, "files": enriched}}

    @mcp.tool()
    async def karox_read_file_range(
        path: str,
        start_line: int = 1,
        end_line: int = 0,
        max_chars: int = 200000,
        line_numbers: bool = True,
    ) -> dict[str, Any]:
        """Read a bounded line range, useful for large source files and precise edits."""
        result = await call("GET", "/file", params={"path": path})
        if not result.get("ok"):
            return result
        payload = _data(result)
        content = payload.get("content")
        if not isinstance(content, str):
            return {"ok": False, "status": 500, "error": "KaroX returned no text content"}
        sliced = _line_slice(content, start_line, end_line, max(1000, min(int(max_chars), 500000)), line_numbers)
        return {"ok": True, "status": result.get("status"), "data": {"path": payload.get("path", path), **sliced}}

    @mcp.tool()
    async def karox_read_files(paths_json: str) -> dict[str, Any]:
        """Read several files in one request. paths_json must be a JSON array of paths."""
        paths, error = _json_array(paths_json, "paths_json")
        if error:
            return error
        if not all(isinstance(item, str) for item in paths or []):
            return {"ok": False, "status": 400, "error": "paths_json must contain only strings"}
        return await call("POST", "/files/read", body={"paths": paths})

    @mcp.tool()
    async def karox_apply_edits(path: str, edits_json: str) -> dict[str, Any]:
        """Apply exact replacements with stale-content checks before writing.

        edits_json is an array like [{"old":"before","new":"after","count":1}].
        Every old string must occur exactly count times or nothing is written.
        """
        edits, error = _json_array(edits_json, "edits_json")
        if error:
            return error
        if not edits:
            return {"ok": False, "status": 400, "error": "edits_json is empty"}
        read = await call("GET", "/file", params={"path": path})
        if not read.get("ok"):
            return read
        original = _data(read).get("content")
        if not isinstance(original, str):
            return {"ok": False, "status": 500, "error": "KaroX returned no text content"}
        updated = original
        applied: list[dict[str, Any]] = []
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict) or not isinstance(edit.get("old"), str) or not isinstance(edit.get("new"), str):
                return {"ok": False, "status": 400, "error": f"Edit {index} must contain string old and new values"}
            old = edit["old"]
            new = edit["new"]
            if not old:
                return {"ok": False, "status": 400, "error": f"Edit {index} has an empty old value"}
            try:
                expected = int(edit.get("count", 1))
            except (TypeError, ValueError):
                return {"ok": False, "status": 400, "error": f"Edit {index} count must be an integer"}
            if expected < 1 or expected > 1000:
                return {"ok": False, "status": 400, "error": f"Edit {index} count must be between 1 and 1000"}
            actual = updated.count(old)
            if actual != expected:
                return {
                    "ok": False,
                    "status": 409,
                    "error": {
                        "code": "stale_or_ambiguous_edit",
                        "editIndex": index,
                        "expectedOccurrences": expected,
                        "actualOccurrences": actual,
                        "hint": "Read the current file range again and submit a more specific old string.",
                    },
                }
            updated = updated.replace(old, new, expected)
            applied.append({"index": index, "replacements": expected})
        if updated == original:
            return {"ok": True, "status": 200, "data": {"path": path, "changed": False, "applied": applied}}
        write = await call("POST", "/file", body={"path": path, "content": updated})
        if not write.get("ok"):
            return write
        return {
            "ok": True,
            "status": write.get("status"),
            "data": {
                "path": path,
                "changed": True,
                "applied": applied,
                "charsBefore": len(original),
                "charsAfter": len(updated),
            },
        }

    @mcp.tool()
    async def karox_run_checks(
        commands_json: str,
        stop_on_failure: bool = True,
        timeout_each_seconds: int = 1800,
    ) -> dict[str, Any]:
        """Run an ordered build/test/lint checklist and summarize every exit code."""
        commands, error = _json_array(commands_json, "commands_json")
        if error:
            return error
        if not commands or not all(isinstance(item, str) and item.strip() for item in commands):
            return {"ok": False, "status": 400, "error": "commands_json must contain non-empty command strings"}
        if len(commands) > 20:
            return {"ok": False, "status": 400, "error": "At most 20 commands are allowed"}
        timeout_each_seconds = max(5, min(int(timeout_each_seconds), 7200))
        results: list[dict[str, Any]] = []
        all_ok = True
        for command in commands:
            result = await call(
                "POST",
                "/run",
                body={"cmd": command, "timeoutSeconds": timeout_each_seconds, "capture": "inline", "tail": 60000},
            )
            exit_code = _data(result).get("exitCode") if result.get("ok") else None
            passed = bool(result.get("ok")) and exit_code == 0
            results.append({"command": command, "passed": passed, "result": result})
            if not passed:
                all_ok = False
                if stop_on_failure:
                    break
        return {"ok": all_ok, "status": 200 if all_ok else 409, "data": {"passed": all_ok, "results": results}}

    setattr(mcp, "_karox_agent_tools_registered", True)
