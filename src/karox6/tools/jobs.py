"""Background jobs: shell commands started with ``run_in_background`` or moved there on timeout."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import time
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


KEEP_FINISHED = 50  # finished, reported jobs kept: a long-lived `karox mcp` server runs many


@dataclass(slots=True)
class JobManager:
    _jobs: dict[str, Job] = field(default_factory=dict)
    _next: int = 1

    def register(self, run: ProcessRun) -> Job:
        done = [k for k, j in self._jobs.items() if j.notified and not j.run.active]
        for key in done[: max(0, len(done) - KEEP_FINISHED)]:
            del self._jobs[key]
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
                f"[background job {job.id} finished ({job.status}): {_short(job.run.command)}."
                f"{_unread_tail(job)}]"
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
        if job.run.active:
            # A read can end inside a multi-byte UTF-8 character; leave that tail for the
            # next read instead of decoding both halves into replacement characters.
            data = data[: len(data) - _incomplete_utf8_tail(data)]
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


NOTICE_TAIL_CHARS = 2_000


def _incomplete_utf8_tail(data: bytes) -> int:
    """Bytes at the end of ``data`` that are a valid but unfinished UTF-8 sequence.
    Invalid bytes (0xFF, a lone 0xC0) are never held back: they surface as U+FFFD."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder.decode(data[-4:], final=False)
    return len(decoder.getstate()[0])


def _unread_tail(job: Job) -> str:
    """The output the model has not read yet, when short; else its last lines. Only a
    complete read moves the job's read offset, so job_output still returns everything."""
    with contextlib.suppress(Exception):
        job.run._flush_log()
    try:
        with job.run.log_path.open("rb") as fh:
            size = fh.seek(0, 2)
            unread = size - job.read_offset
            fh.seek(max(job.read_offset, size - NOTICE_TAIL_CHARS * 4))
            data = fh.read()
    except Exception:  # a notice must never break the step that carries it
        return " Read its output with job_output."
    if unread > NOTICE_TAIL_CHARS * 4:  # long: show the end only, leave the log unread
        tail = clean_text(data).rstrip()[-NOTICE_TAIL_CHARS:]
        tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
        return f" Last lines of {unread} unread bytes (job_output returns all of them):\n{tail}\n"
    text = clean_text(data).rstrip()
    if not text:
        job.read_offset += len(data)
        return " No unread output."
    if len(text) <= NOTICE_TAIL_CHARS:
        job.read_offset += len(data)
        return f" Unread output:\n{text}\n"
    tail = text[-NOTICE_TAIL_CHARS:]
    tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
    return (
        f" Last lines of {len(text)} unread characters (job_output returns all of them):\n{tail}\n"
    )


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
            await job.run.wait(ctx.wait_cap_s(timeout_ms / 1000))
        text = await ctx.jobs.read_new(job, ctx.spill_dir)
        if job.run.returncode is not None:
            job.notified = True
        body = text if text.strip() else "(no new output)"
        return ToolResult(
            f"{body}\n[job {job.id}: {job.status}{_timing(job, ctx)}]",
            meta={"job_id": job.id, "status": job.status},
        )


def _timing(job: Job, ctx: ToolContext) -> str:
    """How long a running job has run and how much of the run is left. Without it a model
    waited 3 x 600 s on a silent full test suite (piped to `tail`) until the run's time
    limit ended the session."""
    if job.status != "running":
        return ""
    note = f", running for {int(job.run.elapsed_s)} s"
    if ctx.deadline is not None:
        left = max(int(ctx.deadline - time.monotonic()), 0)
        note += f"; {left} s left in this run"
    return note


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
        if job.run.returncode is not None and not job.run.active:
            # The shell may have exited while a process it started (`server &`) still
            # runs and holds the output: that one is still stopped below.
            return ToolResult(f"Job {job.id} already finished ({job.status}).")
        job.killed_by_model = True
        await job.run.kill()
        await asyncio.sleep(0)
        return ToolResult(f"Job {job.id} stopped.")
