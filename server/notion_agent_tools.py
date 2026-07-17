"""High-leverage repository tools registered on the KaroX Notion MCP server.

Kept separate from the transport gateway so agent capabilities can evolve without
risking authentication or Streamable HTTP middleware regressions.
"""
from __future__ import annotations

import asyncio
import base64
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


async def _retrying(
    call: CallKaroX,
    method: str,
    path: str,
    *,
    params: Any = None,
    body: Any = None,
    retries: int = 1,
) -> dict[str, Any]:
    """Call KaroX; retry idempotent GETs once and convert transport failures into retryable results."""
    if isinstance(params, dict):
        params = {key: value for key, value in params.items() if value is not None}
    attempt = 0
    while True:
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if body is not None:
            kwargs["body"] = body
        try:
            return await call(method, path, **kwargs)
        except Exception as exc:
            if method == "GET" and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.5)
                continue
            return {"ok": False, "status": 0, "retryable": True, "error": f"KaroX transport failure ({method} {path}): {exc}"}


def _image_content(b64: str, mime: str) -> Any:
    """Return MCP image content when the SDK supports it, else a base64 dict."""
    try:
        from mcp.server.fastmcp import Image  # type: ignore

        fmt = mime.split("/", 1)[1].lower() if "/" in mime else "png"
        return Image(data=base64.b64decode(b64), format=fmt)
    except Exception:
        return {"mimeType": mime, "base64": b64}


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
        regex: bool = False,
        names_only: bool = False,
        extensions: str = "",
        context: int = 0,
        max_size: int = 2000000,
    ) -> dict[str, Any]:
        """Search file contents and optionally return small matching snippets.

        Advanced flags (regex, names_only, extensions, context) switch to the
        search v2 engine with context lines around every match.
        """
        if regex or names_only or extensions.strip() or context:
            return await _retrying(
                call,
                "GET",
                "/search/v2",
                params={
                    "q": query,
                    "regex": regex,
                    "names_only": names_only,
                    "glob": glob or "*",
                    "extensions": extensions or None,
                    "max_size": max_size,
                    "max_files": max(1, min(int(max_files), 500)),
                    "context": context,
                },
            )
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
        commands_json: str = "",
        stop_on_failure: bool = True,
        timeout_each_seconds: int = 1800,
        checks_json: str = "",
    ) -> dict[str, Any]:
        """Run an ordered build/test/lint checklist and summarize every exit code.

        Simple mode: commands_json is a JSON array of command strings.
        Advanced mode: checks_json is an array like
        [{"name","cmd"|"argv","shell","allowFailure","retries","timeoutSeconds"}]
        and runs the v2 matrix with structured error parsing (the summary
        contains firstError with {tool,file,line,message}).
        """
        if checks_json.strip():
            checks, error = _json_array(checks_json, "checks_json")
            if error:
                return error
            return await _retrying(call, "POST", "/checks/v2", body={"checks": checks, "stopOnFailure": stop_on_failure})
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

    # ------------------------------------------------------------------
    # KaroX 4.0 tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def karox_job(
        action: str,
        job_id: str = "",
        argv_json: str = "",
        cmd: str = "",
        shell: str = "",
        name: str = "",
        cwd: str = "",
        lines: int = 100,
        pattern: str = "",
        wait_seconds: float = 0,
        signal: str = "kill",
    ) -> dict[str, Any]:
        """Async jobs: action = start | list | status | tail | signal. tail supports pattern + wait_seconds (follow until pattern)."""
        if action == "start":
            body: dict[str, Any] = {"name": name}
            if argv_json.strip():
                argv, error = _json_array(argv_json, "argv_json")
                if error:
                    return error
                body["argv"] = argv
            if cmd:
                body["cmd"] = cmd
            if shell:
                body["shell"] = shell
            if cwd:
                body["cwd"] = cwd
            return await _retrying(call, "POST", "/jobs/start", body=body)
        if action == "list":
            return await _retrying(call, "GET", "/jobs")
        if not job_id:
            return {"ok": False, "status": 400, "error": "job_id is required for this action"}
        if action == "status":
            return await _retrying(call, "GET", f"/jobs/{job_id}")
        if action == "tail":
            return await _retrying(
                call,
                "GET",
                f"/jobs/{job_id}/tail",
                params={"lines": lines, "wait_seconds": wait_seconds, "pattern": pattern or None},
            )
        if action == "signal":
            return await _retrying(call, "POST", f"/jobs/{job_id}/signal", body={"signal": signal})
        return {"ok": False, "status": 400, "error": f"Unknown action: {action}"}

    @mcp.tool()
    async def karox_wait_for(kind: str, port: int = 0, url: str = "", expect_status: int = 200, timeout_seconds: float = 60) -> dict[str, Any]:
        """Wait until a local port opens (kind="port") or a localhost URL returns expect_status (kind="http")."""
        if kind == "port":
            return await _retrying(call, "GET", "/wait/port", params={"port": port, "timeout_seconds": timeout_seconds})
        if kind == "http":
            return await _retrying(call, "GET", "/wait/http", params={"url": url, "expect_status": expect_status, "timeout_seconds": timeout_seconds})
        return {"ok": False, "status": 400, "error": "kind must be port or http"}

    @mcp.tool()
    async def karox_bytes(action: str, path: str, offset: int = 0, length: int = 1000000, content_base64: str = "", append: bool = False) -> dict[str, Any]:
        """Binary file IO: action="read" returns a base64 chunk with sha256; action="write" writes content_base64."""
        if action == "read":
            return await _retrying(call, "GET", "/bytes", params={"path": path, "offset": offset, "length": length})
        if action == "write":
            return await _retrying(call, "POST", "/bytes", body={"path": path, "contentBase64": content_base64, "append": append})
        return {"ok": False, "status": 400, "error": "action must be read or write"}

    @mcp.tool()
    async def karox_read_image(path: str, max_dimension: int = 1400) -> Any:
        """Read an image file from the repo and return it as viewable MCP image content (the agent's eyes)."""
        result = await _retrying(call, "GET", "/image", params={"path": path, "max_dimension": max_dimension})
        if not result.get("ok"):
            return result
        payload = _data(result)
        b64 = payload.get("contentBase64")
        if not isinstance(b64, str):
            return {"ok": False, "status": 500, "error": "KaroX returned no image content"}
        return _image_content(b64, str(payload.get("mimeType") or "image/png"))

    @mcp.tool()
    async def karox_fs(op: str, src: str = "", dst: str = "", confirm: bool = False, enabled: bool = False, pattern: str = "", max_files: int = 200) -> dict[str, Any]:
        """File ops: move | copy | mkdir | delete_dir (guarded, needs allow_delete_dir + confirm) | allow_delete_dir | glob."""
        if op == "glob":
            return await _retrying(call, "GET", "/fs/glob", params={"pattern": pattern, "max_files": max_files})
        if op == "allow_delete_dir":
            return await _retrying(call, "POST", "/fs/allow-delete-dir", body={"enabled": enabled})
        return await _retrying(call, "POST", "/fs/op", body={"op": op, "src": src or None, "dst": dst or None, "confirm": confirm})

    @mcp.tool()
    async def karox_apply_patch(patch: str, strip_level: int = 1, check_only: bool = False) -> dict[str, Any]:
        """Apply a unified diff to the repo (with secret-scan and a clean-apply pre-check)."""
        return await _retrying(call, "POST", "/patch", body={"patch": patch, "stripLevel": strip_level, "checkOnly": check_only})

    @mcp.tool()
    async def karox_checkpoint(action: str, label: str = "", checkpoint_id: str = "", delete_new_files: bool = False) -> dict[str, Any]:
        """Working-tree checkpoints: create | list | restore. Instant rollback of experiments without commits."""
        if action == "create":
            return await _retrying(call, "POST", "/checkpoint/create", body={"label": label})
        if action == "list":
            return await _retrying(call, "GET", "/checkpoint/list")
        if action == "restore":
            return await _retrying(call, "POST", "/checkpoint/restore", body={"checkpointId": checkpoint_id, "deleteNewFiles": delete_new_files})
        return {"ok": False, "status": 400, "error": "action must be create, list or restore"}

    @mcp.tool()
    async def karox_git2(action: str, params_json: str = "{}") -> dict[str, Any]:
        """Extended git: branch, stash, log, show, blame, restore, diff, merge, rebase, abort, hunks, commit_hunks, secrets_scan.

        params_json is a JSON object passed to the matching /git/v2/* endpoint. Push stays hard-blocked.
        """
        try:
            params = json.loads(params_json) if params_json.strip() else {}
        except json.JSONDecodeError as exc:
            return {"ok": False, "status": 400, "error": f"Invalid params_json: {exc}"}
        if not isinstance(params, dict):
            return {"ok": False, "status": 400, "error": "params_json must decode to an object"}
        gets = {"log": "/git/v2/log", "show": "/git/v2/show", "blame": "/git/v2/blame", "diff": "/git/v2/diff", "hunks": "/git/v2/hunks"}
        posts = {
            "branch": "/git/v2/branch",
            "stash": "/git/v2/stash",
            "restore": "/git/v2/restore",
            "merge": "/git/v2/merge",
            "rebase": "/git/v2/rebase",
            "abort": "/git/v2/abort",
            "commit_hunks": "/git/v2/commit-hunks",
            "secrets_scan": "/secrets/scan",
        }
        if action in gets:
            return await _retrying(call, "GET", gets[action], params=params)
        if action in posts:
            return await _retrying(call, "POST", posts[action], body=params)
        return {"ok": False, "status": 400, "error": f"Unknown action: {action}. GET: {sorted(gets)}; POST: {sorted(posts)}"}

    @mcp.tool()
    async def karox_devserver(
        action: str,
        cmd: str = "",
        argv_json: str = "",
        name: str = "devserver",
        cwd: str = "",
        expected_port: int = 0,
        wait_timeout_seconds: float = 90,
        job_id: str = "",
    ) -> dict[str, Any]:
        """Managed dev-server: start (runs as a job, auto-detects the port from the log) | stop."""
        if action == "start":
            body: dict[str, Any] = {"name": name, "waitTimeoutSeconds": wait_timeout_seconds}
            if argv_json.strip():
                argv, error = _json_array(argv_json, "argv_json")
                if error:
                    return error
                body["argv"] = argv
            if cmd:
                body["cmd"] = cmd
            if cwd:
                body["cwd"] = cwd
            if expected_port:
                body["expectedPort"] = expected_port
            return await _retrying(call, "POST", "/devserver/start", body=body)
        if action == "stop":
            return await _retrying(call, "POST", "/devserver/stop", body={"jobId": job_id})
        return {"ok": False, "status": 400, "error": "action must be start or stop"}

    @mcp.tool()
    async def karox_http_fetch(url: str, method: str = "GET", headers_json: str = "", body: str = "", timeout_seconds: float = 15) -> dict[str, Any]:
        """Fetch a localhost URL: status, headers, body. External addresses are rejected."""
        headers = None
        if headers_json.strip():
            try:
                headers = json.loads(headers_json)
            except json.JSONDecodeError as exc:
                return {"ok": False, "status": 400, "error": f"Invalid headers_json: {exc}"}
        payload: dict[str, Any] = {"url": url, "method": method, "timeoutSeconds": timeout_seconds}
        if headers:
            payload["headers"] = headers
        if body:
            payload["body"] = body
        return await _retrying(call, "POST", "/http/fetch", body=payload)

    @mcp.tool()
    async def karox_browser(
        action: str,
        url: str,
        selector: str = "",
        text: str = "",
        full_page: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        wait_ms: int = 500,
    ) -> Any:
        """Headless browser (Playwright): screenshot | dom | console | click | type. Screenshots come back as viewable images with console errors."""
        result = await _retrying(
            call,
            "POST",
            "/browser",
            body={
                "action": action,
                "url": url,
                "selector": selector or None,
                "text": text if action == "type" else None,
                "fullPage": full_page,
                "viewportWidth": viewport_width,
                "viewportHeight": viewport_height,
                "waitMs": wait_ms,
            },
        )
        if not result.get("ok"):
            return result
        payload = _data(result)
        inner = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        b64 = inner.get("imageBase64")
        if isinstance(b64, str):
            return [
                _image_content(b64, str(inner.get("mimeType") or "image/png")),
                {"console": payload.get("console"), "pageErrors": payload.get("pageErrors")},
            ]
        return result

    @mcp.tool()
    async def karox_pkg(manager: str, action: str, args_json: str = "[]") -> dict[str, Any]:
        """Allowlisted package managers (npm/pnpm/yarn/pip/poetry/cargo/gradle/maven) with lockfile diff report. Publish/auth commands are hard-blocked."""
        args, error = _json_array(args_json or "[]", "args_json")
        if error:
            return error
        return await _retrying(call, "POST", "/pkg/run", body={"manager": manager, "action": action, "args": args})

    @mcp.tool()
    async def karox_screen(
        action: str,
        window_title: str = "",
        region: str = "",
        seconds: float = 3,
        fps: float = 2,
        max_dimension: int = 1600,
        enabled: bool = False,
        input_action: str = "",
        x: int = 0,
        y: int = 0,
        text: str = "",
        key: str = "",
    ) -> Any:
        """Desktop eyes/hands: screenshot | record (gif) | allow_input (session opt-in) | input (opt-in, target window only)."""
        if action == "screenshot":
            result = await _retrying(call, "GET", "/desktop/screenshot", params={"window_title": window_title or None, "region": region or None, "max_dimension": max_dimension})
        elif action == "record":
            result = await _retrying(call, "GET", "/desktop/record", params={"seconds": seconds, "fps": fps, "window_title": window_title or None, "max_dimension": max_dimension})
        elif action == "allow_input":
            return await _retrying(call, "POST", "/desktop/allow-input", body={"enabled": enabled})
        elif action == "input":
            return await _retrying(
                call,
                "POST",
                "/desktop/input",
                body={"windowTitle": window_title, "action": input_action, "x": x, "y": y, "text": text or None, "key": key or None},
            )
        else:
            return {"ok": False, "status": 400, "error": "action must be screenshot, record, allow_input or input"}
        if not result.get("ok"):
            return result
        payload = _data(result)
        b64 = payload.get("contentBase64")
        if isinstance(b64, str):
            return _image_content(b64, str(payload.get("mimeType") or "image/png"))
        return result

    @mcp.tool()
    async def karox_repl(action: str, language: str = "python", repl_id: str = "", code: str = "", timeout_seconds: float = 15) -> dict[str, Any]:
        """Persistent REPL: open (python|node) | eval | close | list. State is kept between eval calls."""
        if action == "open":
            return await _retrying(call, "POST", "/repl/open", body={"language": language})
        if action == "eval":
            return await _retrying(call, "POST", "/repl/eval", body={"replId": repl_id, "code": code, "timeoutSeconds": timeout_seconds})
        if action == "close":
            return await _retrying(call, "POST", f"/repl/{repl_id}/close", body={})
        if action == "list":
            return await _retrying(call, "GET", "/repl/list")
        return {"ok": False, "status": 400, "error": "action must be open, eval, close or list"}

    @mcp.tool()
    async def karox_dap(
        action: str,
        adapter: str = "python",
        session_id: str = "",
        command: str = "",
        arguments_json: str = "",
        adapter_argv_json: str = "",
    ) -> dict[str, Any]:
        """DAP debug bridge: start | request | events | stop. Breakpoints/step/inspect via the Debug Adapter Protocol."""
        if action == "start":
            body: dict[str, Any] = {"adapter": adapter}
            if adapter_argv_json.strip():
                argv, error = _json_array(adapter_argv_json, "adapter_argv_json")
                if error:
                    return error
                body["adapterArgv"] = argv
            return await _retrying(call, "POST", "/dap/start", body=body)
        if action == "request":
            arguments = None
            if arguments_json.strip():
                try:
                    arguments = json.loads(arguments_json)
                except json.JSONDecodeError as exc:
                    return {"ok": False, "status": 400, "error": f"Invalid arguments_json: {exc}"}
            return await _retrying(call, "POST", "/dap/request", body={"sessionId": session_id, "command": command, "arguments": arguments})
        if action == "events":
            return await _retrying(call, "GET", f"/dap/{session_id}/events")
        if action == "stop":
            return await _retrying(call, "POST", f"/dap/{session_id}/stop", body={})
        return {"ok": False, "status": 400, "error": "action must be start, request, events or stop"}

    @mcp.tool()
    async def karox_project_map(refresh: bool = False) -> dict[str, Any]:
        """Auto project map: project kind, entry points, build/test/run commands, top-level directories."""
        return await _retrying(call, "GET", "/context/project-map", params={"refresh": refresh})

    @mcp.tool()
    async def karox_memo(action: str, key: str = "", value: str = "") -> dict[str, Any]:
        """Per-repo long-term memory: set | get | list | delete. Survives restarts and sessions."""
        if action == "set":
            return await _retrying(call, "POST", "/memo/set", body={"key": key, "value": value})
        if action == "get":
            return await _retrying(call, "GET", "/memo/get", params={"key": key})
        if action == "list":
            return await _retrying(call, "GET", "/memo/list")
        if action == "delete":
            return await _retrying(call, "POST", "/memo/delete", body={"key": key})
        return {"ok": False, "status": 400, "error": "action must be set, get, list or delete"}

    @mcp.tool()
    async def karox_workspace(action: str, name: str = "", path: str = "") -> dict[str, Any]:
        """Multi-repo: list | register | switch (switch requires KAROX_ALLOW_WORKSPACE_SWITCH=1 on the server)."""
        if action == "list":
            return await _retrying(call, "GET", "/workspace/list")
        if action == "register":
            return await _retrying(call, "POST", "/workspace/register", body={"name": name, "path": path})
        if action == "switch":
            return await _retrying(call, "POST", "/workspace/switch", body={"name": name})
        return {"ok": False, "status": 400, "error": "action must be list, register or switch"}

    @mcp.tool()
    async def karox_session(action: str, session_id: str = "", switch_branch: bool = True) -> dict[str, Any]:
        """Persistent sessions: list | persisted | resume (same branch/task after a restart) | ping (watchdog heartbeat)."""
        if action == "list":
            return await _retrying(call, "GET", "/session/list")
        if action == "persisted":
            return await _retrying(call, "GET", "/session/persisted")
        if action == "resume":
            return await _retrying(call, "POST", "/session/resume", body={"sessionId": session_id or None, "switchBranch": switch_branch})
        if action == "ping":
            return await _retrying(call, "GET", "/watchdog/ping")
        return {"ok": False, "status": 400, "error": "action must be list, persisted, resume or ping"}

    @mcp.tool()
    async def karox_events(after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        """Poll server events (job exits, devserver ready, failed checks) newer than after_id."""
        return await _retrying(call, "GET", "/events/poll", params={"after_id": after_id, "limit": limit})

    setattr(mcp, "_karox_agent_tools_registered", True)
