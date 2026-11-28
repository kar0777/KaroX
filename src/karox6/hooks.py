"""Hooks: your commands that run at points of the agent loop (the Claude Code format).

``<KaroX home>/hooks.json`` and each enabled plugin's ``hooks/hooks.json``::

    {"hooks": {
      "PreToolUse":  [{"matcher": "Bash|Write", "hooks": [{"type": "command",
                        "command": "python ~/check.py", "timeout": 30}]}],
      "PostToolUse": [...], "UserPromptSubmit": [...], "Stop": [...], "SessionStart": [...]
    }}

A hook gets a JSON object on stdin (``session_id``, ``cwd``, ``hook_event_name``, and per
event ``tool_name`` / ``tool_input`` / ``tool_response``, ``prompt``,
``last_assistant_message``, ``stop_hook_active``, ``source``) and answers with its exit code:

- 0: fine; stdout may be JSON (``{"decision": "block", "reason": ...}``,
  ``{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": ...,
  "additionalContext": ...}}``); for UserPromptSubmit and SessionStart plain stdout is
  context the model gets with the message;
- 2: block; stderr is the reason (a tool call is not run and the model reads why, a
  prompt is not sent, the model may not stop yet);
- anything else: the hook failed; the user sees a warning and the loop goes on.

``{"continue": false, "stopReason": ...}`` ends the run. A PreToolUse hook may rewrite the
call with ``hookSpecificOutput.updatedInput``. SessionStart's matcher is on ``source``
(``startup`` / ``resume``).

``matcher`` is a regular expression on the tool's name; KaroX's names (``bash``,
``read``, ``write``, ``edit``, ``glob``, ``grep``, ``mcp__server__tool``) and Claude Code's
(``Bash``, ``Read``, ``Write``, ``Edit``, ``Glob``, ``Grep``) both match; ``tool_name`` on
stdin is the Claude Code name when there is one (``karox_tool_name`` is KaroX's). Commands
run in the shell the agent uses, in the workspace, with CLAUDE_PROJECT_DIR /
KAROX_PROJECT_DIR set (and CLAUDE_PLUGIN_ROOT for a plugin's hooks: the shell expands
``${CLAUDE_PLUGIN_ROOT}``, so quote it like any variable). Project folders never add hooks: a
repository could run commands on your machine. Off with ``hooks = false`` (or
KAROX_HOOKS=0).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.tools.proc import kill_tree
from karox6.tools.shell import ShellInfo

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart")
FILE_NAME = "hooks.json"
DEFAULT_TIMEOUT_S = 60.0
MAX_OUTPUT_CHARS = 10_000
_CLAUDE_NAMES = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "apply_patch": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "todo_write": "TodoWrite",
    "agent": "Task",
    "skill": "Skill",
}


@dataclass(frozen=True, slots=True)
class Hook:
    event: str
    matcher: str  # "" or "*": every tool
    command: str
    timeout_s: float
    source: str  # user | plugin:<name>
    root: str = ""  # the plugin's folder


@dataclass(slots=True)
class Outcome:
    block: bool = False
    reason: str = ""
    context: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    halt: bool = False  # {"continue": false}: end the run (reason: stopReason)
    updated_input: dict[str, Any] | None = None  # PreToolUse: run the call with these


def enabled(setting: Any = None) -> bool:
    wanted = os.environ.get("KAROX_HOOKS", "")
    if wanted in ("0", "1"):
        return wanted == "1"
    return setting is not False


def _parse(path: Path, source: str, root: str, warn: list[str]) -> list[Hook]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return []
    except ValueError as exc:
        warn.append(f"{path}: not JSON ({exc})")
        return []
    table = data.get("hooks", data) if isinstance(data, dict) else None
    if not isinstance(table, dict):
        warn.append(f"{path}: 'hooks' must be an object")
        return []
    found: list[Hook] = []
    for event, groups in table.items():
        if event not in EVENTS:
            if isinstance(groups, list):
                warn.append(f"{path}: {event} hooks are not supported by KaroX (left out)")
            continue
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            matcher = str(group.get("matcher") or "")
            for hook in group.get("hooks") or []:
                if not isinstance(hook, dict) or hook.get("type", "command") != "command":
                    continue
                command = str(hook.get("command") or "").strip()
                if not command:
                    continue
                timeout = hook.get("timeout")
                seconds = (
                    float(timeout)
                    if isinstance(timeout, int | float) and not isinstance(timeout, bool)
                    else DEFAULT_TIMEOUT_S
                )
                found.append(Hook(event, matcher, command, max(1.0, seconds), source, root))
    return found


def load(home: Path | None, warn: list[str] | None = None) -> list[Hook]:
    """The user's hooks, then each enabled plugin's."""
    if home is None:
        return []
    from karox6.plugins import plugin_dirs

    problems = warn if warn is not None else []
    hooks = _parse(home / FILE_NAME, "user", "", problems)
    for folder in plugin_dirs(home):
        hooks += _parse(
            folder / "hooks" / FILE_NAME, f"plugin:{folder.name}", folder.as_posix(), problems
        )
    return hooks


def _matches(matcher: str, tool: str) -> bool:
    if matcher in ("", "*"):
        return True
    names = [tool, _CLAUDE_NAMES.get(tool, tool)]
    try:
        pattern = re.compile(matcher)
    except re.error:
        return matcher in names
    return any(pattern.fullmatch(name) for name in names)


class Hooks:
    def __init__(
        self, hooks: list[Hook], *, session_id: str, workspace: Path, shell: ShellInfo
    ) -> None:
        self.hooks = hooks
        self.session_id = session_id
        self.workspace = workspace
        self.shell = shell

    def __bool__(self) -> bool:
        return bool(self.hooks)

    def has(self, event: str) -> bool:
        return any(h.event == event for h in self.hooks)

    async def run(
        self,
        event: str,
        payload: dict[str, Any],
        tool: str | None = None,
        match: str | None = None,
    ) -> Outcome:
        """Run every hook of ``event`` whose matcher fits ``tool`` (or ``match``, e.g.
        SessionStart's source) one after another."""
        outcome = Outcome()
        target = tool if tool is not None else match
        chosen = [
            h
            for h in self.hooks
            if h.event == event and (target is None or _matches(h.matcher, target))
        ]
        if not chosen:
            return outcome
        body = {
            "session_id": self.session_id,
            "cwd": str(self.workspace),
            "hook_event_name": event,
            **payload,
        }
        if tool is not None:
            body["tool_name"] = _CLAUDE_NAMES.get(tool, tool)  # what Claude Code hooks expect
            body["karox_tool_name"] = tool
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        for hook in chosen:
            code, out, err = await self._exec(hook, data)
            label = f"{event} hook ({hook.source}) `{hook.command[:60]}`"
            if code is None:
                outcome.warnings.append(f"{label} took longer than {hook.timeout_s:.0f}s: stopped")
                continue
            if code == 2:
                outcome.block = True
                outcome.reason = (err or out or "blocked by a hook").strip()[:MAX_OUTPUT_CHARS]
                break
            if code != 0:
                detail = (err or out).strip().splitlines()[-1:] or [""]
                outcome.warnings.append(f"{label} failed (exit {code}) {detail[0][:200]}")
                continue
            self._read_answer(event, out, outcome)
            if outcome.block or outcome.halt:
                break
        return outcome

    @staticmethod
    def _read_answer(event: str, out: str, outcome: Outcome) -> None:
        text = out.strip()
        if not text:
            return
        answer: Any = None
        if text.startswith("{"):
            with contextlib.suppress(ValueError):
                answer = json.loads(text)
        if not isinstance(answer, dict):
            if event in ("UserPromptSubmit", "SessionStart"):
                outcome.context.append(text[:MAX_OUTPUT_CHARS])
            return
        specific = answer.get("hookSpecificOutput")
        specific = specific if isinstance(specific, dict) else {}
        decision = str(answer.get("decision") or "").lower()
        permission = str(specific.get("permissionDecision") or "").lower()
        if answer.get("continue") is False:
            outcome.halt = True
            outcome.reason = str(answer.get("stopReason") or answer.get("reason") or "")
        elif decision in ("block", "deny") or permission == "deny":
            outcome.block = True
            outcome.reason = str(
                answer.get("reason") or specific.get("permissionDecisionReason") or ""
            )
        updated = specific.get("updatedInput")
        if event == "PreToolUse" and isinstance(updated, dict) and not outcome.block:
            outcome.updated_input = updated
        context = specific.get("additionalContext")
        if isinstance(context, str) and context.strip():
            outcome.context.append(context.strip()[:MAX_OUTPUT_CHARS])

    async def _exec(self, hook: Hook, data: bytes) -> tuple[int | None, str, str]:
        env = dict(os.environ)
        env.update(
            CLAUDE_PROJECT_DIR=str(self.workspace),
            KAROX_PROJECT_DIR=str(self.workspace),
            PYTHONIOENCODING="utf-8",
        )
        if hook.root:
            env.update(CLAUDE_PLUGIN_ROOT=hook.root, KAROX_PLUGIN_ROOT=hook.root)
        script: Path | None = None
        if self.shell.flavor == "git-bash":  # a script file keeps backslashes intact
            fd, name = tempfile.mkstemp(prefix="karox-hook-", suffix=".sh")
            os.close(fd)
            script = Path(name)
        try:
            return await self._spawn(hook, data, env, script)
        finally:
            if script is not None:
                with contextlib.suppress(OSError):
                    script.unlink()

    def _command(self, hook: Hook) -> str:
        command = hook.command
        if self.shell.flavor in ("pwsh", "powershell"):  # sh flavors expand ${VAR} themselves
            for var in ("CLAUDE_PLUGIN_ROOT", "KAROX_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR"):
                command = command.replace("${" + var + "}", "$env:" + var)
        return command

    async def _spawn(
        self, hook: Hook, data: bytes, env: dict[str, str], script: Path | None
    ) -> tuple[int | None, str, str]:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # its own group: a timeout kills the whole tree
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.shell.argv(self._command(hook), script),
                cwd=str(self.workspace),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError as exc:
            return 1, "", f"cannot start: {exc}"
        talk = asyncio.ensure_future(proc.communicate(data))
        try:
            out, err = await asyncio.wait_for(asyncio.shield(talk), hook.timeout_s)
        except TimeoutError:
            await _stop(proc, talk)
            return None, "", ""
        except asyncio.CancelledError:
            await asyncio.shield(_stop(proc, talk))
            raise
        return proc.returncode, _text(out), _text(err)


async def _stop(proc: asyncio.subprocess.Process, talk: asyncio.Future[Any]) -> None:
    """Kill the hook and whatever it started; wait a little for the pipes to close."""
    await asyncio.to_thread(kill_tree, proc.pid)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(talk), 5.0)
    if not talk.done():
        talk.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await talk


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")[:MAX_OUTPUT_CHARS]
