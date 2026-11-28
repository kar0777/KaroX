"""``page_screenshot``: open a page in the installed Chrome / Edge / Chromium (headless, on the
real GPU) and return the picture plus the page's console messages.

No Playwright, no server: one browser process per call with a throwaway profile, at
most one at a time, below-normal priority on Windows, a hard time limit that kills the
whole process tree, and the profile folder removed afterwards. Agents building web pages,
canvas or WebGL scenes used to assemble their own headless browsers with software
rendering (about a second per frame, colours that differ from a real GPU) and spent
hundreds of steps on pixel statistics instead of looking.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from karox6.lifetime import join_karox_job
from karox6.tools.base import ToolContext, ToolResult
from karox6.tools.proc import kill_tree

TIME_LIMIT_S = 90.0
DEFAULT_WAIT_MS = 2500
MAX_WAIT_MS = 20_000
MAX_SIDE = 2560
MAX_CONSOLE_LINES = 30
MAX_SCRIPT_CHARS = 20_000
COPY_PREFIX = ".karox-shot-"
STDERR_KEEP = 256 * 1024  # bytes of the browser's log kept (console lines are in it)
JOB_MEMORY_MB = 2048  # Windows: the browser's whole process tree may not commit more
# new Chromium: `:CONSOLE:5] "text", source: url (5)`; older: `:CONSOLE(5)] ...`
# A message may span lines (an error's stack): it ends at `", source: ...`.
_CONSOLE = re.compile(
    r':CONSOLE(?::\d+|\(\d+\))?\]\s*"(?P<text>.*?)",\s*source:\s*(?P<source>[^\r\n]*)',
    re.DOTALL,
)

_WINDOWS_PATHS = (
    r"Google\Chrome\Application\chrome.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"Chromium\Application\chrome.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
)
_MAC_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
_UNIX_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
)


def find_browser() -> str | None:
    """A Chromium-family browser: KAROX_BROWSER, then the usual install places. Only
    looks at files (no process is started)."""
    wanted = os.environ.get("KAROX_BROWSER", "").strip()
    if wanted:
        if wanted in ("0", "off", "none"):
            return None
        return wanted if Path(wanted).is_file() else shutil.which(wanted)
    if sys.platform == "win32":
        roots = [
            os.environ.get(name, "")
            for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        for root in filter(None, roots):
            for tail in _WINDOWS_PATHS:
                candidate = Path(root) / tail
                if candidate.is_file():
                    return str(candidate)
        return None
    if sys.platform == "darwin":
        for path in _MAC_PATHS:
            if Path(path).is_file():
                return path
    for name in _UNIX_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def console_lines(log: str) -> list[str]:
    """The page's console messages (and uncaught errors) from Chromium's stderr log."""
    found: list[str] = []
    for match in _CONSOLE.finditer(log):
        source = match["source"].strip()
        where = re.sub(r"^.*/", "", source)  # "file:///C:/x/page.html (12)" -> "page.html (12)"
        lines = [ln.strip() for ln in match["text"].splitlines() if ln.strip()]
        found.append(f"{' | '.join(lines)[:600]}  [{where[:80]}]")
    return found


class PageScreenshotTool:
    name = "page_screenshot"
    description = (
        "Open a web page (an .html file in the workspace or an http(s) URL, e.g. your dev "
        "server) in a real headless browser on the machine's GPU and look at it: returns a "
        "PNG screenshot and the page's console messages and uncaught errors. Use it to check "
        "what you build for the browser (layout, canvas, WebGL/Three.js scenes, animations) "
        "instead of guessing from code or pixel numbers; take several with different "
        "`wait_ms` to see motion. The page runs `wait_ms` of page time before the shot "
        "(timers and requestAnimationFrame advance). One call takes a few seconds. "
        "`script` runs your JavaScript in the page after its own scripts (move the camera, "
        "hide objects, console.log values: the output comes back), so prefer this tool to "
        "writing Playwright or Puppeteer scripts; the page must expose what you touch, e.g. "
        "window.debug = { camera, scene }. A file target may carry ?query or #hash. If you "
        "still need Playwright, launch it with channel 'chrome' (GPU): its bundled headless "
        "shell renders WebGL on the CPU, slowly, with different colours, loading the machine."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Path of an .html file (relative to the workspace) or an "
                "http(s):// URL.",
            },
            "width": {"type": "integer", "description": "Viewport width in px (default 1280)."},
            "height": {"type": "integer", "description": "Viewport height in px (default 800)."},
            "wait_ms": {
                "type": "integer",
                "description": f"Page time before the shot (default {DEFAULT_WAIT_MS}, max "
                f"{MAX_WAIT_MS}).",
            },
            "script": {
                "type": "string",
                "description": "JavaScript run as a module after the page's own scripts "
                "(import and await allowed), before `wait_ms` elapses; errors and console "
                "output are reported. Workspace files only; it runs on a copy of the page "
                "next to it (.karox-shot-*.html), so location differs.",
            },
        },
        "required": ["target"],
    }
    read_only = False  # starts a browser; never two at once

    def __init__(self, browser: str) -> None:
        self.browser = browser
        self._lock: asyncio.Lock | None = None

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(args.get("target") or "").strip()
        if not raw:
            return ToolResult("Error: `target` is empty.", is_error=True)
        path: Path | None = None
        suffix = ""
        if re.match(r"^https?://", raw, re.IGNORECASE):
            url, shown = raw, raw
        else:
            path = ctx.resolve(raw)
            if not path.is_file():
                match = re.match(r"^(?P<file>[^?#]+)(?P<rest>[?#].*)$", raw)
                path = ctx.resolve(match["file"]) if match else path
                suffix = match["rest"] if match else ""
            if not path.is_file():
                return ToolResult(f"Error: {raw} is not a file.", is_error=True)
            url, shown = path.resolve().as_uri() + suffix, raw
        width = _clamp(args.get("width"), 1280, 200, MAX_SIDE)
        height = _clamp(args.get("height"), 800, 200, MAX_SIDE)
        wait_ms = _clamp(args.get("wait_ms"), DEFAULT_WAIT_MS, 0, MAX_WAIT_MS)
        script = str(args.get("script") or "")
        copy: Path | None = None
        if script.strip():
            if path is None:
                return ToolResult(
                    "Error: `script` works on workspace files; for a URL, expose a debug "
                    "view through its query string or use Playwright with channel 'chrome'.",
                    is_error=True,
                )
            if len(script) > MAX_SCRIPT_CHARS:
                return ToolResult(
                    f"Error: `script` is longer than {MAX_SCRIPT_CHARS} characters.",
                    is_error=True,
                )
            # A copy next to the page (relative imports and assets still resolve) with the
            # script appended; removed after the shot.
            page = path.resolve()
            _remove_stale_copies(page.parent)
            copy = page.with_name(f"{COPY_PREFIX}{os.urandom(4).hex()}.html")
            try:
                copy.write_bytes(_with_script(page.read_bytes(), script))
            except OSError as exc:
                with contextlib.suppress(OSError):
                    copy.unlink()
                return ToolResult(f"Error: cannot prepare the page: {exc}", is_error=True)
            url = copy.as_uri() + suffix
        if self._lock is None:
            self._lock = asyncio.Lock()
        try:
            async with self._lock:  # one browser at a time: a few hundred MB each
                result = await self._shoot(url, shown, width, height, wait_ms)
            if copy is not None and path is not None:  # console sources name the page
                result.content = result.content.replace(copy.name, path.name)
            return result
        finally:
            if copy is not None:
                with contextlib.suppress(OSError):
                    copy.unlink()

    async def _shoot(self, url: str, shown: str, width: int, height: int, wait: int) -> ToolResult:
        folder = Path(tempfile.mkdtemp(prefix="karox-shot-"))
        try:
            png = folder / "shot.png"
            argv = [
                self.browser,
                "--headless=new",
                f"--user-data-dir={folder / 'profile'}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--mute-audio",
                "--hide-scrollbars",
                "--js-flags=--max-old-space-size=1024",  # a runaway page cannot eat the RAM
                "--enable-logging=stderr",
                "--v=0",
                f"--window-size={width},{height}",
                f"--virtual-time-budget={wait}",
                f"--screenshot={png}",
            ]
            if url.startswith("file:"):
                argv.append("--allow-file-access-from-files")  # its own local ES modules
            argv.append(url)
            code, log = await _run(argv, folder)
            if code is None:
                return ToolResult(
                    f"Error: the browser did not finish within {TIME_LIMIT_S:.0f}s (stopped). "
                    "A page that never goes idle (endless network requests) cannot be "
                    "captured; try a smaller wait_ms.",
                    is_error=True,
                )
            messages = console_lines(log)
            try:
                data = png.read_bytes()
            except OSError:
                tail = " ".join(log.strip().splitlines()[-3:])[:400]
                return ToolResult(
                    f"Error: no screenshot (browser exit {code}). {tail}", is_error=True
                )
        finally:
            await asyncio.to_thread(_remove, folder)
        lines = [f"Screenshot of {shown} ({width}x{height}, after {wait} ms of page time)."]
        if messages:
            lines.append(f"Console ({len(messages)} message(s)):")
            lines += [f"- {m}" for m in messages[:MAX_CONSOLE_LINES]]
            if len(messages) > MAX_CONSOLE_LINES:
                lines.append(f"- ... {len(messages) - MAX_CONSOLE_LINES} more")
        else:
            lines.append("Console: no messages, no uncaught errors.")
        lines.append("The picture follows this result.")
        return ToolResult(
            "\n".join(lines),
            meta={"image_png": data, "console_messages": len(messages)},
        )


def _with_script(html: bytes, script: str) -> bytes:
    """The page's bytes as they are, then ``script`` as its last module script: after
    the end of the document a parser still puts it in the body, and nothing of the page
    is searched, decoded or re-encoded. Top level, so `import` and `await` work; an
    error shows in the console as an uncaught one."""
    body = script.replace("</script", "<\\/script")
    return html + b'\n<script type="module">\n' + body.encode("utf-8") + b"\n</script>\n"


def _remove_stale_copies(folder: Path) -> None:
    """Copies left by a KaroX that was killed during a shot."""
    cutoff = time.time() - 600
    with contextlib.suppress(OSError):
        for stale in folder.glob(f"{COPY_PREFIX}*.html"):
            with contextlib.suppress(OSError):
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(low, min(high, int(value)))


async def _run(argv: list[str], cwd: Path) -> tuple[int | None, str]:
    """Run the browser; (exit code or None after the time limit, the tail of its stderr).

    Everything it starts dies with it: on Windows a job object that kills the whole tree
    when closed (and caps its memory), elsewhere its own process group."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # below normal even when commands keep the normal priority: it is ours
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        )
    else:
        kwargs["start_new_session"] = True
    join_karox_job()  # Windows: it also dies with KaroX, however KaroX ends
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except OSError as exc:
        return 1, f"cannot start the browser: {exc}"
    job = _Job.attach(proc.pid) if sys.platform == "win32" else None
    tail = _Tail()
    reader = asyncio.ensure_future(tail.pump(proc.stderr))
    try:
        try:
            await asyncio.wait_for(_exited(proc), TIME_LIMIT_S)
        except TimeoutError:
            return None, ""
        # Its output is complete once every process holding the pipe is gone.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), 5.0)
        return proc.returncode, tail.text()
    finally:
        await _settle(_stop(proc, job, reader))


async def _settle(cleanup: Any) -> None:
    """Run ``cleanup`` to the end even when the caller is cancelled (again) meanwhile."""
    task = asyncio.ensure_future(cleanup)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


async def _stop(
    proc: asyncio.subprocess.Process, job: _Job | None, reader: asyncio.Future[Any]
) -> None:
    if job is not None:
        job.close()  # kills every process of the tree, the ones that outlived the leader too
    elif sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, 9)  # the group outlives its leader; SIGKILL
    if proc.returncode is None:
        await asyncio.to_thread(kill_tree, proc.pid)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(_exited(proc), 5.0)
    # With the tree gone the pipe reaches its end: let the reader and the transport
    # finish, then close what is still open (an unclosed pipe transport is a leak).
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(reader), 3.0)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 3.0)
    if not reader.done():
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


class _Tail:
    """The last STDERR_KEEP bytes of a stream: a page that logs without end costs no
    more memory than that."""

    def __init__(self) -> None:
        self.data = bytearray()

    async def pump(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65536):
            self.data += chunk
            if len(self.data) > 2 * STDERR_KEEP:
                del self.data[:-STDERR_KEEP]

    def text(self) -> str:
        return bytes(self.data[-STDERR_KEEP:]).decode("utf-8", "replace")


def _remove(folder: Path) -> None:
    """Delete the throwaway profile; a browser process still closing may hold a file for
    a moment on Windows."""
    for attempt in range(5):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(0.2 * (attempt + 1))


class _Job:
    """A Windows job object holding the browser: closing it ends the whole process tree
    (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), and the tree may not commit more than
    JOB_MEMORY_MB. Children are put in the job as they start, since the browser itself
    is assigned a few milliseconds after it starts, before it spawns any."""

    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def attach(cls, pid: int) -> _Job | None:
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:  # pragma: no cover
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic),
                ("IoInfo", _Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _Extended()
        kill_on_close, job_memory = 0x2000, 0x200
        info.BasicLimitInformation.LimitFlags = kill_on_close | job_memory
        info.JobMemoryLimit = JOB_MEMORY_MB * 1024 * 1024
        extended_limit_information = 9
        ok = kernel32.SetInformationJobObject(
            wintypes.HANDLE(job),
            extended_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        process_set_quota_terminate = 0x0100 | 0x0001
        process = kernel32.OpenProcess(process_set_quota_terminate, False, pid)
        if not ok or not process:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            if process:
                kernel32.CloseHandle(wintypes.HANDLE(process))
            return None
        assigned = kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(process))
        kernel32.CloseHandle(wintypes.HANDLE(process))
        if not assigned:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
        return cls(job)

    def close(self) -> None:
        if not self.handle:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined,unused-ignore]
        kernel32.TerminateJobObject(wintypes.HANDLE(self.handle), 1)
        kernel32.CloseHandle(wintypes.HANDLE(self.handle))
        self.handle = 0


async def _exited(proc: asyncio.subprocess.Process) -> int:
    """Wait for the process itself to end. ``proc.wait()`` also waits for its pipes to
    close, which a child that outlives the browser keeps open until it is killed."""
    while proc.returncode is None:
        await asyncio.sleep(0.1)
    return proc.returncode
