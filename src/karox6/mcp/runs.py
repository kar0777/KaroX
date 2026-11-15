"""Runs started by MCP clients: background sessions, event logs, approvals, idempotency.

A client starts a run, then polls ``status`` (long-poll with ``wait_s``) until it is
no longer running. Dangerous commands wait for ``approve`` (or are denied after
``approval_timeout_s``). A repeated ``start`` with the same ``request_id`` returns the
existing run, so a client that reconnects and retries never starts the work twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.core.events import Event
from karox6.core.session import Session, SessionConfig
from karox6.core.store import SessionStore
from karox6.providers.base import Provider
from karox6.tools.base import ApprovalRequest, Tool
from karox6.tools.files import is_generated

MAX_EVENTS = 4000
MAX_RUNS = 200
FIELD_CAP = 2000
DIFF_CAP = 40_000
WAIT_CAP_S = 50.0
SKIPPED_EVENTS = frozenset({"text_delta", "reasoning_delta", "usage"})


class RunError(Exception):
    """A client mistake (unknown run, bad workspace...): reported as a tool error."""


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    request: ApprovalRequest
    future: asyncio.Future[bool]

    def view(self) -> dict[str, str]:
        return {
            "approval_id": self.approval_id,
            "tool": self.request.tool,
            "command": self.request.summary,
            "rule": self.request.rule,
            "reason": self.request.reason,
        }


@dataclass(slots=True)
class Run:
    run_id: str
    session: Session
    task: str
    request_id: str | None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"
    reason: str = ""
    final_text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    pending: dict[str, PendingApproval] = field(default_factory=dict)
    worker: asyncio.Task[None] | None = None
    tick: asyncio.Event = field(default_factory=asyncio.Event)
    approvals: int = 0

    @property
    def running(self) -> bool:
        return self.status == "running"

    def notify(self) -> None:
        self.tick.set()
        self.tick = asyncio.Event()

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session.session_id,
            "status": self.status,
            "workspace": str(self.session.workspace),
            "task": _cap(self.task, 300),
            "started": _iso(self.started),
            "finished": _iso(self.finished) if self.finished else None,
            "pending_approvals": [p.view() for p in self.pending.values()],
        }


ConfigFactory = Callable[[Path], SessionConfig]
ProviderFactory = Callable[[], Provider]


class RunManager:
    def __init__(
        self,
        config_factory: ConfigFactory,
        provider_factory: ProviderFactory,
        *,
        root: Path,
        any_workspace: bool = False,
        approval_mode: str = "ask",
        approval_timeout_s: float = 600.0,
        home: Path | None = None,
        extra_tools: list[Tool] | None = None,
    ) -> None:
        self.config_factory = config_factory
        self.provider_factory = provider_factory
        self.root = root.resolve()
        self.any_workspace = any_workspace
        self.approval_mode = approval_mode
        self.approval_timeout_s = approval_timeout_s
        self.home = home
        self.extra_tools = list(extra_tools or [])
        self.runs: dict[str, Run] = {}
        self.by_request: dict[str, str] = {}
        self._seq = 0

    # -- commands -------------------------------------------------------------------

    def start(
        self,
        task: str,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[Run, bool]:
        """Start a run (or return the run already started for ``request_id``)."""
        task = task.strip()
        if request_id and request_id in self.by_request:
            existing = self.runs[self.by_request[request_id]]
            if existing.task != task:
                raise RunError(f"request_id {request_id!r} was already used for another task")
            return existing, True
        if not task:
            raise RunError("task is empty")
        workspace = self._workspace(cwd)
        busy = [r for r in self.runs.values() if r.running and r.session.workspace == workspace]
        if busy:
            raise RunError(
                f"run {busy[0].run_id} is still working in {workspace}; wait for it, "
                "cancel it, or use another directory"
            )
        config = self.config_factory(workspace)
        kwargs: dict[str, Any] = {}
        if session_id:
            if any(r.running and r.session.session_id == session_id for r in self.runs.values()):
                raise RunError(f"session {session_id} is already running")
            try:
                loaded = SessionStore.load(config.home, session_id)
            except FileNotFoundError as exc:
                raise RunError(str(exc)) from None
            kwargs = {
                "session_id": session_id,
                "messages": loaded.messages,
                "usage": loaded.usage,
                "cost_usd": loaded.cost_usd,
            }
        self._seq += 1
        run_id = f"run-{self._seq}-{secrets.token_hex(3)}"
        holder: list[Run] = []

        async def approver(request: ApprovalRequest) -> bool:
            return await self._approve(holder[0], request)

        session = Session(
            config,
            self.provider_factory(),
            approver=approver,
            extra_tools=self.extra_tools,
            **kwargs,
        )
        run = Run(run_id=run_id, session=session, task=task, request_id=request_id)
        holder.append(run)
        self.runs[run_id] = run
        if request_id:
            self.by_request[request_id] = run_id
        run.worker = asyncio.get_running_loop().create_task(self._drive(run))
        self._forget_old_runs()
        return run, False

    def _forget_old_runs(self) -> None:
        """Keep at most MAX_RUNS runs in memory, dropping the oldest finished ones."""
        excess = len(self.runs) - MAX_RUNS
        for old in [r for r in self.runs.values() if not r.running][: max(excess, 0)]:
            del self.runs[old.run_id]
            if old.request_id:
                self.by_request.pop(old.request_id, None)

    async def status(self, run_id: str, *, since: int = 0, wait_s: float = 0.0) -> dict[str, Any]:
        run = self.get(run_id)
        since = max(int(since), 0)
        wait_s = float(wait_s)
        wait_s = min(max(wait_s, 0.0), WAIT_CAP_S) if math.isfinite(wait_s) else 0.0
        await asyncio.sleep(0)  # let the run make progress even for a non-waiting poll
        if wait_s and run.running and self._next(run) <= since:
            tick = run.tick
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(tick.wait(), timeout=wait_s)
        first = max(since - run.dropped, 0)
        events = run.events[first:]
        view = run.summary()
        view.update(
            events=events,
            next=self._next(run),
            final_text=run.final_text if not run.running else "",
            reason=run.reason,
            usage=run.session.usage.to_dict(),
            cost_usd=round(run.session.cost_usd, 6),
            steps=sum(1 for e in run.events if e.get("type") == "assistant_message"),
        )
        if run.dropped and since < run.dropped:
            view["note"] = f"{run.dropped} oldest events were dropped from memory"
        return view

    def approve(self, run_id: str, approval_id: str, approved: bool) -> dict[str, Any]:
        run = self.get(run_id)
        pending = run.pending.get(approval_id)
        if pending is None:
            return {
                "run_id": run_id,
                "approval_id": approval_id,
                "resolved": False,
                "note": "no such pending approval (already answered or timed out)",
            }
        if not pending.future.done():
            pending.future.set_result(bool(approved))
        return {
            "run_id": run_id,
            "approval_id": approval_id,
            "resolved": True,
            "approved": bool(approved),
        }

    async def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.get(run_id)
        if run.running and run.worker is not None:
            run.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run.worker
        return run.summary()

    async def files(self, run_id: str) -> dict[str, Any]:
        run = self.get(run_id)
        changed = await asyncio.to_thread(run.session.files.changed_files)
        return {"run_id": run_id, "workspace": str(run.session.workspace), "files": changed}

    async def diff(self, run_id: str, *, max_chars: int = DIFF_CAP) -> dict[str, Any]:
        run = self.get(run_id)
        workspace = run.session.workspace
        text, untracked = await asyncio.to_thread(git_diff, workspace)
        if text is None:
            changed = await asyncio.to_thread(run.session.files.changed_files)
            return {
                "run_id": run_id,
                "git": False,
                "files": changed,
                "diff": "",
                "note": "workspace is not a git repository: files only",
            }
        cap = min(max(int(max_chars), 1000), 200_000)
        truncated = len(text) > cap
        return {
            "run_id": run_id,
            "git": True,
            "diff": text[:cap],
            "truncated": truncated,
            "total_chars": len(text),
            "untracked": untracked,
        }

    def list(self) -> list[dict[str, Any]]:
        return [r.summary() for r in sorted(self.runs.values(), key=lambda r: r.started)]

    def get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise RunError(
                f"unknown run_id {run_id!r} (runs live in this server process only; "
                "continue an old session with karox_run session_id)"
            )
        return run

    async def shutdown(self) -> None:
        for run in list(self.runs.values()):
            if run.running and run.worker is not None:
                run.worker.cancel()
        for run in list(self.runs.values()):
            if run.worker is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run.worker

    # -- internals ------------------------------------------------------------------

    def _workspace(self, cwd: str | None) -> Path:
        if not cwd:
            return self.root
        path = Path(cwd).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not self.any_workspace and not path.is_relative_to(self.root):
            raise RunError(f"cwd {path} is outside the server root {self.root}")
        if not path.is_dir():
            raise RunError(f"cwd {path} is not a directory")
        return path

    async def _drive(self, run: Run) -> None:
        try:
            async for event in run.session.run(run.task):
                self._record(run, event)
                if event.type == "run_finished":
                    run.status = getattr(event, "status", "error")
                    run.reason = getattr(event, "reason", "")
                    run.final_text = getattr(event, "final_text", "")
        except asyncio.CancelledError:
            run.status, run.reason = "cancelled", "cancelled by the client"
        except Exception as exc:
            run.status, run.reason = "error", f"{type(exc).__name__}: {exc}"
        finally:
            if run.status == "running":
                run.status = "error"
                run.reason = run.reason or "run ended without a final event"
            for pending in run.pending.values():
                if not pending.future.done():
                    pending.future.set_result(False)
            run.finished = time.time()
            run.notify()

    def _record(self, run: Run, event: Event) -> None:
        if event.type in SKIPPED_EVENTS:
            return
        data = {k: _shrink(v) for k, v in event.to_dict().items()}
        data["i"] = self._next(run)
        run.events.append(data)
        if len(run.events) > MAX_EVENTS:
            excess = len(run.events) - MAX_EVENTS
            del run.events[:excess]
            run.dropped += excess
        run.notify()

    @staticmethod
    def _next(run: Run) -> int:
        return run.dropped + len(run.events)

    async def _approve(self, run: Run, request: ApprovalRequest) -> bool:
        # Session emits ApprovalRequested("approval-N") right before calling us, in order.
        run.approvals += 1
        approval_id = f"approval-{run.approvals}"
        if self.approval_mode == "allow":
            return True
        if self.approval_mode != "ask":
            return False
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        run.pending[approval_id] = PendingApproval(approval_id, request, future)
        run.notify()
        try:
            return await asyncio.wait_for(future, timeout=self.approval_timeout_s)
        except TimeoutError:
            return False
        finally:
            run.pending.pop(approval_id, None)
            run.notify()


def git_diff(workspace: Path) -> tuple[str | None, list[str]]:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    try:
        inside = git("rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return None, []
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None, []
    diff = git("diff", "HEAD", "--no-color", "--", ".")
    if diff.returncode != 0:  # no commits yet
        diff = git("diff", "--no-color", "--", ".")
    untracked = git("ls-files", "--others", "--exclude-standard", "--", ".")
    paths = [line for line in untracked.stdout.splitlines() if line and not is_generated(line)]
    return diff.stdout, paths[:500]


def _shrink(value: Any) -> Any:
    if isinstance(value, str):
        return _cap(value, FIELD_CAP)
    if isinstance(value, list):
        return [_shrink(v) for v in value[:50]]
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    return value


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40] + f"... [{len(text) - limit + 40} chars cut]"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
