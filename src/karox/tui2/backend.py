"""Subprocess bridge to the KaroX CLI.

Every surface the client needs already has a machine-readable form:
``agent run --stream-json``, ``model list --json``, ``session list --json``,
``mcp status --json``. Keeping the UI on that boundary means it can be tested
against a fake process, a model call can never block the render loop, and a
crashed run leaves a resumable session on disk instead of taking the screen
down with it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..detached_process import windowless_flags
from .protocol import Event, Outcome, decode_line

_STDERR_TAIL = 4000


class BackendError(RuntimeError):
    """A CLI surface failed; the message is already safe to show."""


@dataclass(frozen=True)
class AgentRequest:
    repository: Path
    task: str
    session_id: str
    continue_task: bool = False
    verification: tuple[tuple[str, ...], ...] = ()
    effort: str | None = None
    mode: str | None = None


@dataclass
class StreamResult:
    outcome: Outcome | None
    exit_code: int
    stderr: str = ""
    stopped: bool = False
    events: list[Event] = field(default_factory=list)


class CLIBackend:
    """Run the installed CLI with the current interpreter."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository.expanduser().resolve()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stopping = False

    # ------------------------------------------------------------------ calls

    def call(
        self,
        *arguments: str,
        input_text: str | None = None,
        timeout: float = 60.0,
    ) -> Any:
        command = self._argv(arguments)
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._environment(),
                creationflags=windowless_flags(),
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"command timed out after {timeout:.0f}s: {' '.join(arguments)}") from exc
        except OSError as exc:
            raise BackendError(f"cannot start KaroX CLI: {exc}") from exc
        output = (completed.stdout or "").strip()
        error = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = _clean_error(error or output)
            raise BackendError(detail or f"command failed with exit code {completed.returncode}")
        if not output:
            return None
        try:
            return json.loads(output)
        except (ValueError, RecursionError) as exc:
            raise BackendError(f"expected JSON from {' '.join(arguments)}: {output[:500]}") from exc

    # ---------------------------------------------------------------- stream

    def stream_agent(
        self,
        request: AgentRequest,
        on_event: Callable[[Event], None],
    ) -> StreamResult:
        arguments = [
            "agent",
            "run",
            "--repository",
            str(request.repository),
            "--session-id",
            request.session_id,
            "--task",
            request.task,
            "--stream-json",
        ]
        if request.continue_task:
            arguments.append("--continue-task")
        if request.effort:
            arguments.extend(("--effort-level", request.effort))
        if request.mode:
            arguments.extend(("--mode", request.mode))
        for command in request.verification:
            arguments.extend(
                ("--verification-command", json.dumps(list(command), ensure_ascii=False))
            )
        with self._lock:
            self._stopping = False
            try:
                process = subprocess.Popen(
                    self._argv(arguments),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=self._environment(),
                    creationflags=self._creation_flags(),
                    start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                return StreamResult(None, -1, f"cannot start KaroX CLI: {exc}")
            self._process = process
        stderr_chunks: list[str] = []

        def drain() -> None:
            if process.stderr is None:
                return
            for chunk in process.stderr:
                stderr_chunks.append(chunk)

        watcher = threading.Thread(target=drain, name="karox-tui-stderr", daemon=True)
        watcher.start()
        outcome: Outcome | None = None
        events: list[Event] = []
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    decoded = decode_line(line)
                    if decoded is None:
                        continue
                    if decoded.kind == "event" and decoded.event is not None:
                        events.append(decoded.event)
                        on_event(decoded.event)
                    elif decoded.kind == "result" and decoded.outcome is not None:
                        outcome = decoded.outcome
            exit_code = process.wait()
        finally:
            with self._lock:
                self._process = None
            watcher.join(timeout=2.0)
        return StreamResult(
            outcome=outcome,
            exit_code=int(exit_code),
            stderr="".join(stderr_chunks).strip()[-_STDERR_TAIL:],
            stopped=self._stopping,
            events=events,
        )

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._stopping = True
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
        except (OSError, ValueError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass

    # --------------------------------------------------------------- domains

    def models(self) -> list[dict[str, Any]]:
        payload = self.call("model", "list", "--json")
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def selected_model(self) -> dict[str, Any] | None:
        for item in self.models():
            if item.get("selected"):
                return item
        return None

    def select_model(self, provider_id: str, model_id: str) -> Any:
        return self.call("model", "select", provider_id, model_id, "--json")

    def sessions(self) -> list[dict[str, Any]]:
        payload = self.call("session", "list", "--json")
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def session(self, session_id: str) -> dict[str, Any] | None:
        try:
            payload = self.call("session", "show", session_id, "--json")
        except BackendError:
            return None
        return payload if isinstance(payload, dict) else None

    def provider_presets(self) -> list[dict[str, Any]]:
        payload = self.call("provider", "presets", "--json")
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def provider_setup(
        self,
        preset_id: str,
        *,
        model_id: str,
        api_key: str,
        provider_id: str = "",
        base_url: str = "",
    ) -> dict[str, Any]:
        arguments = [
            "provider",
            "setup",
            preset_id,
            "--model",
            model_id,
            "--stdin-key",
            "--json",
        ]
        if provider_id:
            arguments.extend(("--provider-id", provider_id))
        if base_url:
            arguments.extend(("--base-url", base_url))
        payload = self.call(*arguments, input_text=api_key + "\n", timeout=120.0)
        return payload if isinstance(payload, dict) else {}

    def mcp_status(self, session_id: str | None = None) -> dict[str, Any]:
        arguments = ["mcp", "status", "--repository", str(self.repository), "--json"]
        if session_id and self.session(session_id) is not None:
            arguments.extend(("--session-id", session_id))
        payload = self.call(*arguments)
        return payload if isinstance(payload, dict) else {}

    def mcp_add(
        self,
        *,
        server_id: str,
        namespace: str,
        transport: str,
        command: str = "",
        url: str = "",
        arguments: Sequence[str] = (),
    ) -> dict[str, Any]:
        argv = [
            "mcp",
            "server",
            "add",
            server_id,
            "--namespace",
            namespace,
            "--transport",
            transport,
            "--json",
        ]
        if transport == "stdio":
            argv.extend(("--command", command))
            for item in arguments:
                argv.extend(("--arg", item))
        else:
            argv.extend(("--url", url))
        payload = self.call(*argv)
        return payload if isinstance(payload, dict) else {}

    def mcp_remove(self, server_id: str) -> Any:
        return self.call("mcp", "server", "remove", server_id, "--json")

    def mcp_probe(self, server_id: str) -> dict[str, Any]:
        payload = self.call(
            "mcp",
            "server",
            "doctor",
            server_id,
            "--repository",
            str(self.repository),
            "--json",
            timeout=90.0,
        )
        return payload if isinstance(payload, dict) else {}

    def mcp_select_all(self, server_id: str, session_id: str) -> dict[str, Any]:
        discovery = self.call(
            "mcp",
            "server",
            "inspect",
            server_id,
            "--repository",
            str(self.repository),
            "--json",
            timeout=90.0,
        )
        tools = []
        if isinstance(discovery, dict):
            tools = [
                str(item.get("remote_name"))
                for item in discovery.get("tools", [])
                if isinstance(item, dict) and item.get("remote_name")
            ]
        arguments = [
            "mcp",
            "session",
            "select",
            server_id,
            "--session-id",
            session_id,
            "--repository",
            str(self.repository),
            "--json",
        ]
        for name in tools:
            arguments.extend(("--permission", f"{name}=allow"))
        payload = self.call(*arguments, timeout=120.0)
        return payload if isinstance(payload, dict) else {}

    def mcp_deselect(self, server_id: str, session_id: str) -> Any:
        return self.call(
            "mcp",
            "session",
            "deselect",
            server_id,
            "--session-id",
            session_id,
            "--repository",
            str(self.repository),
            "--json",
        )

    # ------------------------------------------------------------- internals

    def _argv(self, arguments: Iterable[str]) -> list[str]:
        return [sys.executable, "-m", "karox.cli", *arguments]

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        source_root = Path(__file__).resolve().parents[2]
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            f"{source_root}{os.pathsep}{existing}" if existing else str(source_root)
        )
        return environment

    @staticmethod
    def _creation_flags() -> int:
        flags = windowless_flags()
        if os.name == "nt":
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        return flags


def _clean_error(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-8:])[:2000]