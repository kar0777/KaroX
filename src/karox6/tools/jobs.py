"""Background jobs: shell commands started with ``run_in_background`` or moved there on timeout."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.context.spill import limit_output
from karox6.tools.base import ToolContext, ToolResult
from karox6.tools.proc import ProcessRun, clean_text

READ_CAP_CHARS = 30_000


@dataclass(slots=True)
class Job:
    id: str
    run: ProcessRun
    read_offset: int = 0
    notified: bool = False
    killed_by_model: bool = False

    @property
    def status(self) -> str:
        code = self.run.returncode
        if code is None or self.run.active:
            return "running"
        if self.run.killed:
            return "killed"
        return f"exited with code {code}"


@dataclass(slots=True)
class JobManager:
    _jobs: dict[str, Job] = field(default_factory=dict)
    _next: int = 1

    def register(self, run: ProcessRun) -> Job:
        job = Job(id=str(self._next), run=run)
        self._next += 1
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(str(job_id).strip())

    def running(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.run.active]

    def drain_finished(self) -> list[str]:
        """One line per job that finished since the last call (model-killed jobs are silent)."""
        notes = []
        for job in self._jobs.values():
            if job.notified or job.run.active:
                continue
            job.notified = True
            if job.killed_by_model:
                continue
            notes.append(
                f"[background job {job.id} finished ({job.status}): {_short(job.run.command)}. "
                "Read its output with job_output.]"
            )
        return notes

    async def read_new(self, job: Job, spill_dir: Path) -> str:
        job.run._flush_log()
        try:
            with job.run.log_path.open("rb") as fh:
                fh.seek(job.read_offset)
                data = fh.read()
        except OSError as exc:
            return f"(cannot read job log: {exc})"
        job.read_offset += len(data)
        text = clean_text(data)
        inline, _ = limit_output(
            text,
            spill_dir=spill_dir,
            name=f"job{job.id}",
            max_chars=READ_CAP_CHARS,
            full_path=job.run.log_path,
        )
        return inline

    async def shutdown(self) -> None:
        for job in self._jobs.values():
            with contextlib.suppress(Exception):
                await job.run.kill()


def _short(command: str, limit: int = 80) -> str:
    one = " ".join(command.split())
    return one if len(one) <= limit else one[: limit - 3] + "..."


class JobOutputTool:
    name = "job_output"
    description = (
        "Read a background job's new output since the previous read, plus its status. "
        "Set wait: true (or a timeout) to block until the job finishes or the timeout (ms, "
        "default 30000, max 600000) passes, instead of polling; use it only when you are "
        "genuinely blocked on the job."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Id returned when the job started."},
            "wait": {"type": "boolean", "description": "Wait for the job to finish."},
            "timeout": {
                "type": "integer",
                "description": "Wait limit in ms; giving one waits unless wait is false.",
            },
        },
        "required": ["job_id"],
    }
    read_only = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        job = ctx.jobs.get(str(args["job_id"]))
        if job is None:
            return ToolResult(f"Error: no background job {args['job_id']!r}.", is_error=True)
        # A timeout means the caller wants to wait: 56 of 90 bench calls passed one without
        # wait: true and came straight back, then polled again and again.
        wants_wait = args.get("wait") is not False and bool(args.get("wait") or args.get("timeout"))
        if wants_wait and job.run.returncode is None:
            timeout_ms = min(max(int(args.get("timeout") or 30_000), 1), 600_000)
            await job.run.wait(timeout_ms / 1000)
        text = await ctx.jobs.read_new(job, ctx.spill_dir)
        if job.run.returncode is not None:
            job.notified = True
        body = text if text.strip() else "(no new output)"
        return ToolResult(
            f"{body}\n[job {job.id}: {job.status}]", meta={"job_id": job.id, "status": job.status}
        )


class JobListTool:
    name = "job_list"
    description = "List your background jobs (running and finished) with ids and statuses."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    read_only = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        jobs = list(ctx.jobs._jobs.values())
        if not jobs:
            return ToolResult("No background jobs.")
        lines = [
            f"{j.id}: {j.status}, {j.run.elapsed_s:.0f}s, pid {j.run.pid}: {_short(j.run.command)}"
            for j in jobs
        ]
        return ToolResult("\n".join(lines))


class JobKillTool:
    name = "job_kill"
    description = "Stop a running background job and its child processes."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    }
    read_only = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        job = ctx.jobs.get(str(args["job_id"]))
        if job is None:
            return ToolResult(f"Error: no background job {args['job_id']!r}.", is_error=True)
        if job.run.returncode is not None:
            return ToolResult(f"Job {job.id} already finished ({job.status}).")
        job.killed_by_model = True
        await job.run.kill()
        await asyncio.sleep(0)
        return ToolResult(f"Job {job.id} stopped.")
