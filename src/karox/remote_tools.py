"""Extended local-only tools used by a remote Ellipsis reasoning session.

The cloud agent never receives a filesystem.  These handlers execute on the
user's machine inside the same CoreRuntime policy/idempotency boundary as the
native KaroX tools.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import httpx

from .core import (
    CoreError,
    InvalidCommand,
    ProcessTree,
    ToolDefinition,
    VerificationRule,
    _new_process_group_kwargs,
)
from .core_tools import ExtendedCoreRuntime
from .models import Capability, CoreCommand, CoreResult, EvidenceRecord
from .paths import runtime_dir
from .security import child_process_environment, redact, redact_content
from .sessions import SessionRecord


_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# The separator classes deliberately include whitespace and the dot/colon used
# by task runners. The previous pattern required one of `-_/` on both sides, so
# it matched `run-deploy` but not `npm run deploy`, `npm run build:publish`, or a
# bare `deploy` argument -- exactly the spellings a real project uses. Anchoring
# on a non-word boundary instead keeps `redeployment` and `pushover` allowed
# while blocking every separated form.
_BLOCKED_WORDS = re.compile(
    r"(?:^|(?<=[\s\-_/.:]))"
    r"(push|publish|deploy|login|logout|auth|release)"
    r"(?:$|(?=[\s\-_/.:]))",
    re.IGNORECASE,
)
_SHELLS = frozenset({"bash", "cmd", "dash", "fish", "nu", "sh", "wsl", "zsh"})
_POWERSHELL = frozenset({"powershell", "pwsh"})
_MAX_PROCESS_OUTPUT = 1_000_000


@dataclass(frozen=True)
class ManagedProcessRecord:
    process_id: str
    pid: int
    session_id: str
    argv: tuple[str, ...]
    started_at: float
    stdout_path: str
    stderr_path: str


class ManagedProcessStore:
    def __init__(self, session_id: str) -> None:
        self.root = (
            runtime_dir() / "vnext" / "ellipsis" / "processes" / session_id
        ).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, process_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", process_id):
            raise InvalidCommand("process_id is malformed")
        return self.root / f"{process_id}.json"

    def put(self, record: ManagedProcessRecord) -> None:
        path = self.path(record.process_id)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(asdict(record), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def get(self, process_id: str) -> ManagedProcessRecord:
        try:
            payload = json.loads(self.path(process_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise InvalidCommand("managed process does not exist") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CoreError("managed process metadata is unreadable") from exc
        try:
            return ManagedProcessRecord(
                process_id=str(payload["process_id"]),
                pid=int(payload["pid"]),
                session_id=str(payload["session_id"]),
                argv=tuple(str(item) for item in payload["argv"]),
                started_at=float(payload["started_at"]),
                stdout_path=str(payload["stdout_path"]),
                stderr_path=str(payload["stderr_path"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CoreError("managed process metadata is malformed") from exc

    def list(self) -> tuple[ManagedProcessRecord, ...]:
        result: list[ManagedProcessRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                result.append(self.get(path.stem))
            except CoreError:
                continue
        return tuple(result)


def _basename(argv0: str) -> str:
    name = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in completed.stdout and "No tasks" not in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _kill_pid_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        return
    time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass


def _read_log(path: Path, limit: int = 100_000) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    total = len(raw)
    truncated = total > limit
    if truncated:
        raw = raw[-limit:]
    return {
        "text": str(redact(raw.decode("utf-8", errors="replace"))),
        "bytes": total,
        "truncated": truncated,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class RemoteCoreRuntime(ExtendedCoreRuntime):
    """Core runtime with guarded commands, processes and localhost browser tools."""

    MAX_BROWSER_BYTES = 2_000_000
    MAX_BROWSER_ACTIONS = 30

    def __init__(
        self,
        *args: Any,
        command_commands: Optional[Iterable[Iterable[str]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._command_rules = tuple(
            VerificationRule.parse(item) for item in (command_commands or ())
        )
        self._handlers = dict(self._handlers)
        self._handlers.update(
            {
                "command.run": self._command_run,
                "process.start": self._process_start,
                "process.status": self._process_status,
                "process.stop": self._process_stop,
                "browser.fetch": self._browser_fetch,
                "browser.actions": self._browser_actions,
                "report.get": self._report_get,
            }
        )
        self._definitions.update(
            {
                "command.run": ToolDefinition(
                    "command.run",
                    "Run one user-approved local command without a shell.",
                    Capability.PROCESS_RUN,
                    True,
                    {
                        "type": "object",
                        "properties": {
                            "argv": {"type": "array", "items": {"type": "string"}},
                            "timeout_seconds": {"type": "number"},
                        },
                        "required": ["argv"],
                        "additionalProperties": False,
                    },
                    replayable=False,
                ),
                "process.start": ToolDefinition(
                    "process.start",
                    "Start one user-approved managed local process or dev server.",
                    Capability.PROCESS_RUN,
                    True,
                    {
                        "type": "object",
                        "properties": {
                            "argv": {"type": "array", "items": {"type": "string"}},
                            "process_id": {"type": "string"},
                        },
                        "required": ["argv"],
                        "additionalProperties": False,
                    },
                ),
                "process.status": ToolDefinition(
                    "process.status",
                    "Read status and bounded logs of a managed process.",
                    Capability.PROCESS_RUN,
                    False,
                    {
                        "type": "object",
                        "properties": {"process_id": {"type": "string"}},
                        "required": ["process_id"],
                        "additionalProperties": False,
                    },
                ),
                "process.stop": ToolDefinition(
                    "process.stop",
                    "Stop a managed local process tree.",
                    Capability.PROCESS_RUN,
                    True,
                    {
                        "type": "object",
                        "properties": {"process_id": {"type": "string"}},
                        "required": ["process_id"],
                        "additionalProperties": False,
                    },
                ),
                "browser.fetch": ToolDefinition(
                    "browser.fetch",
                    "Fetch a localhost-only URL for browser verification.",
                    Capability.BROWSER_READ,
                    False,
                    {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "max_bytes": {"type": "number"},
                        },
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                ),
                "browser.actions": ToolDefinition(
                    "browser.actions",
                    "Run bounded Playwright actions against localhost and optionally save a screenshot.",
                    Capability.BROWSER_INPUT,
                    True,
                    {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "actions": {"type": "array", "items": {"type": "object"}},
                            "screenshot_path": {"type": "string"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                        },
                        "required": ["url", "actions"],
                        "additionalProperties": False,
                    },
                ),
                "report.get": ToolDefinition(
                    "report.get",
                    "Return the local session report, changed files, checks and Git evidence.",
                    Capability.GIT_READ,
                    False,
                    {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                ),
            }
        )

    def _preflight_mutation(
        self,
        command_name: str,
        arguments: dict[str, Any],
        deadline_seconds: float,
    ) -> None:
        if command_name in {"command.run", "process.start"}:
            argv = self._required(arguments, "argv", list)
            self._guarded_argv(argv)
            return
        if command_name == "process.stop":
            self._required(arguments, "process_id", str)
            return
        if command_name == "browser.actions":
            self._prepare_browser_actions(arguments)
            return
        super()._preflight_mutation(command_name, arguments, deadline_seconds)

    def _record_mutation(
        self, record: SessionRecord, command: CoreCommand, result: CoreResult
    ) -> None:
        super()._record_mutation(record, command, result)
        if command.name == "browser.actions":
            path = result.data.get("screenshot_path")
            if isinstance(path, str) and path not in record.changed_files:
                record.changed_files.append(path)

    def _guarded_argv(self, raw: Sequence[Any]) -> list[str]:
        if (
            not raw
            or len(raw) > 100
            or not all(isinstance(item, str) and item for item in raw)
            or any("\0" in item or len(item) > 10_000 for item in raw)
        ):
            raise InvalidCommand("argv must contain 1-100 valid strings")
        argv = list(raw)
        executable = _basename(argv[0])
        if executable == "git" or executable in _SHELLS:
            raise InvalidCommand(
                "shells and Git commands are not available through command.run"
            )
        joined = " ".join(argv[:8])
        if _BLOCKED_WORDS.search(joined):
            raise InvalidCommand(
                "publishing, deployment and authentication commands are blocked"
            )
        if executable in _POWERSHELL:
            lowered = [item.lower() for item in argv[1:]]
            forbidden = {"-command", "-c", "-encodedcommand", "-enc"}
            if forbidden.intersection(lowered):
                raise InvalidCommand(
                    "PowerShell is allowed only with a repository script via -File"
                )
            try:
                index = lowered.index("-file")
            except ValueError as exc:
                raise InvalidCommand(
                    "PowerShell is allowed only with a repository script via -File"
                ) from exc
            if index + 2 >= len(argv):
                raise InvalidCommand("PowerShell -File requires a script path")
            script = argv[index + 2]
            resolved = self.safe_path(script)
            if resolved.suffix.lower() != ".ps1" or not resolved.is_file():
                raise InvalidCommand(
                    "PowerShell -File must reference an existing repository .ps1 file"
                )
        if not self._command_rules:
            raise InvalidCommand(
                "no user-approved command allowlist was configured for this session"
            )
        if not any(rule.matches(argv) for rule in self._command_rules):
            raise InvalidCommand(
                "command is not in the user-approved command allowlist"
            )
        return argv

    def _command_run(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        argv = self._guarded_argv(self._required(arguments, "argv", list))
        requested = float(arguments.get("timeout_seconds", 120.0))
        if requested <= 0:
            raise InvalidCommand("timeout_seconds must be positive")
        result = self._run(argv, min(requested, deadline_seconds))
        result["_evidence"] = [
            EvidenceRecord(
                kind="command",
                summary=("Passed" if result["exit_code"] == 0 else "Failed")
                + f": {' '.join(result['argv'])}",
                command=result["argv"],
                exit_code=result["exit_code"],
                metadata={"timed_out": result["timed_out"]},
            )
        ]
        return result

    def _process_start(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        argv = self._guarded_argv(self._required(arguments, "argv", list))
        process_id = arguments.get("process_id")
        if process_id is None:
            process_id = f"proc-{uuid.uuid4().hex[:12]}"
        if not isinstance(process_id, str):
            raise InvalidCommand("process_id must be string")
        store = ManagedProcessStore(self._active_session_id)
        try:
            existing = store.get(process_id)
        except InvalidCommand:
            existing = None
        if existing is not None and _pid_alive(existing.pid):
            return {
                "process_id": existing.process_id,
                "pid": existing.pid,
                "running": True,
                "started_at": existing.started_at,
                "_evidence": [
                    EvidenceRecord(
                        kind="process",
                        summary=f"Managed process already running: {process_id}",
                        command=list(redact(existing.argv)),
                    )
                ],
            }
        stdout_path = store.root / f"{process_id}.stdout.log"
        stderr_path = store.root / f"{process_id}.stderr.log"
        stdout_handle = stdout_path.open("ab", buffering=0)
        stderr_handle = stderr_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.repository,
                env=child_process_environment(),
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                **_new_process_group_kwargs(),
            )
        except Exception:
            stdout_handle.close()
            stderr_handle.close()
            raise
        stdout_handle.close()
        stderr_handle.close()
        record = ManagedProcessRecord(
            process_id=process_id,
            pid=process.pid,
            session_id=self._active_session_id,
            argv=tuple(argv),
            started_at=time.time(),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        store.put(record)
        return {
            "process_id": process_id,
            "pid": process.pid,
            "running": True,
            "started_at": record.started_at,
            "_evidence": [
                EvidenceRecord(
                    kind="process",
                    summary=f"Started managed process: {process_id}",
                    command=list(redact(argv)),
                    metadata={"pid": process.pid},
                )
            ],
        }

    @property
    def _active_session_id(self) -> str:
        value = getattr(self, "_executing_session_id", None)
        if not isinstance(value, str) or not value:
            raise CoreError("remote process operation has no active session")
        return value

    def execute(
        self,
        command: CoreCommand,
        capability_token: Optional[str] = None,
        lease: Any = None,
    ) -> CoreResult:
        self._executing_session_id = command.session_id
        try:
            return super().execute(command, capability_token, lease)
        finally:
            self._executing_session_id = None

    def _process_status(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        process_id = self._required(arguments, "process_id", str)
        record = ManagedProcessStore(self._active_session_id).get(process_id)
        return {
            "process_id": process_id,
            "pid": record.pid,
            "running": _pid_alive(record.pid),
            "started_at": record.started_at,
            "stdout": _read_log(Path(record.stdout_path)),
            "stderr": _read_log(Path(record.stderr_path)),
        }

    def _process_stop(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        process_id = self._required(arguments, "process_id", str)
        record = ManagedProcessStore(self._active_session_id).get(process_id)
        was_running = _pid_alive(record.pid)
        if was_running:
            _kill_pid_tree(record.pid)
        return {
            "process_id": process_id,
            "pid": record.pid,
            "was_running": was_running,
            "running": _pid_alive(record.pid),
            "_evidence": [
                EvidenceRecord(
                    kind="process",
                    summary=f"Stopped managed process: {process_id}",
                    command=list(redact(record.argv)),
                    metadata={"pid": record.pid, "was_running": was_running},
                )
            ],
        }

    @staticmethod
    def _local_url(value: Any) -> str:
        if not isinstance(value, str) or len(value) > 4096:
            raise InvalidCommand("url must be string")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.hostname.lower() not in _LOCAL_HOSTS
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise InvalidCommand("browser tools are limited to localhost URLs")
        return value

    def _browser_fetch(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        url = self._local_url(arguments.get("url"))
        requested = arguments.get("max_bytes", self.MAX_BROWSER_BYTES)
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise InvalidCommand("max_bytes must be number")
        limit = min(max(int(requested), 1), self.MAX_BROWSER_BYTES)
        try:
            with httpx.Client(
                timeout=min(deadline_seconds, 30.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            raise CoreError("localhost browser fetch failed") from exc
        body = response.content
        clipped = len(body) > limit
        returned = body[:limit]
        return {
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
            "content": str(redact_content(returned.decode("utf-8", errors="replace"))),
            "bytes": len(body),
            "truncated": clipped,
            "sha256": hashlib.sha256(body).hexdigest(),
        }

    def _prepare_browser_actions(
        self, arguments: Mapping[str, Any]
    ) -> tuple[str, list[Mapping[str, Any]], Optional[Path], int, int]:
        url = self._local_url(arguments.get("url"))
        actions = arguments.get("actions")
        if not isinstance(actions, list) or len(actions) > self.MAX_BROWSER_ACTIONS:
            raise InvalidCommand(
                f"actions must be an array with at most {self.MAX_BROWSER_ACTIONS} entries"
            )
        normalized: list[Mapping[str, Any]] = []
        for item in actions:
            if not isinstance(item, Mapping):
                raise InvalidCommand("each browser action must be an object")
            kind = item.get("type")
            if kind not in {"click", "fill", "press", "wait", "assert_text"}:
                raise InvalidCommand(f"unsupported browser action: {kind}")
            selector = item.get("selector")
            if kind != "wait" and (
                not isinstance(selector, str) or not selector or len(selector) > 2000
            ):
                raise InvalidCommand("browser action selector is malformed")
            if kind in {"fill", "press", "assert_text"}:
                value = item.get("value")
                if not isinstance(value, str) or len(value) > 100_000:
                    raise InvalidCommand("browser action value is malformed")
            normalized.append(dict(item))
        screenshot: Optional[Path] = None
        raw_path = arguments.get("screenshot_path")
        if raw_path is not None:
            if not isinstance(raw_path, str):
                raise InvalidCommand("screenshot_path must be string")
            screenshot = self.safe_path(raw_path, for_write=True)
            if screenshot.suffix.lower() != ".png":
                raise InvalidCommand("screenshot_path must end in .png")
        width = int(arguments.get("width", 1440))
        height = int(arguments.get("height", 900))
        if not 320 <= width <= 3840 or not 240 <= height <= 2160:
            raise InvalidCommand("browser viewport is outside safe bounds")
        return url, normalized, screenshot, width, height

    def _browser_actions(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        url, actions, screenshot, width, height = self._prepare_browser_actions(arguments)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CoreError(
                "browser automation requires the optional Playwright package and browser"
            ) from exc
        observations: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": height})

                def route_local(route: Any) -> None:
                    target = urlsplit(route.request.url)
                    if target.hostname and target.hostname.lower() in _LOCAL_HOSTS:
                        route.continue_()
                    else:
                        route.abort()

                page.route("**/*", route_local)
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=min(30_000, int(deadline_seconds * 1000)),
                )
                for item in actions:
                    kind = str(item["type"])
                    if kind == "click":
                        page.locator(str(item["selector"])).click()
                    elif kind == "fill":
                        page.locator(str(item["selector"])).fill(str(item["value"]))
                    elif kind == "press":
                        page.locator(str(item["selector"])).press(str(item["value"]))
                    elif kind == "wait":
                        page.wait_for_timeout(min(int(item.get("milliseconds", 250)), 5000))
                    else:
                        text = page.locator(str(item["selector"])).inner_text()
                        expected = str(item["value"])
                        observations.append(
                            {
                                "selector": str(item["selector"]),
                                "expected": expected,
                                "matched": expected in text,
                            }
                        )
                        if expected not in text:
                            raise CoreError("browser text assertion failed")
                if screenshot is not None:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot), full_page=True)
                title = page.title()
                final_url = page.url
                self._local_url(final_url)
            finally:
                browser.close()
        result: dict[str, Any] = {
            "action_count": len(actions),
            "title": str(redact(title)),
            "final_url": final_url,
            "observations": observations,
            "_evidence": [
                EvidenceRecord(
                    kind="browser",
                    summary=f"Completed {len(actions)} localhost browser action(s)",
                    metadata={"screenshot": screenshot is not None},
                )
            ],
        }
        if screenshot is not None:
            raw = screenshot.read_bytes()
            result["screenshot_path"] = screenshot.relative_to(self.repository).as_posix()
            result["screenshot_sha256"] = hashlib.sha256(raw).hexdigest()
        return result

    def _report_get(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        if arguments:
            raise InvalidCommand("report.get takes no arguments")
        record = self.sessions.load(self._active_session_id)
        self.sessions.validate_repository(record, self.repository)
        status = self._git(["status", "--short", "--branch"], deadline_seconds)
        diff = self._git(["diff", "--no-ext-diff"], deadline_seconds)
        return {
            "session_id": record.session_id,
            "task": record.task,
            "branch": record.branch,
            "access_profile": record.access_profile,
            "status": record.status,
            "changed_files": list(record.changed_files),
            "checks": list(record.checks),
            "evidence_count": len(record.evidence),
            "git_status": status,
            "git_diff": diff,
        }
