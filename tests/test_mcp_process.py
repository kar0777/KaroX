"""`karox6 mcp` as a real process: when its client goes away, no command outlives it.

A fake OpenAI-compatible model asks for one long `bash` command; the test then stops
the server the way MCP clients do (close stdin; SIGTERM on POSIX) and checks that the
server exits promptly and the command's process is gone.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


class _FakeModel(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        server: Any = self.server
        server.calls += 1
        if server.calls == 1:
            arguments = json.dumps({"command": server.command, "timeout": 600000})
            function = {"name": "bash", "arguments": arguments}
            call = {"id": "c1", "type": "function", "function": function}
            message: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "done"}, "stop"
        body = json.dumps(
            {
                "id": "x",
                "object": "chat.completion",
                "model": "m",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


class _Server:
    """`karox6 mcp` over stdio, with a fake model whose first answer runs a long command."""

    def __init__(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()
        self.pid_file = self.workspace / "pid.txt"
        code = (
            "import os, time; "
            f"open({str(self.pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(300)"
        )
        python = shlex.quote(sys.executable.replace("\\", "/"))
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
        self.model.calls = 0  # type: ignore[attr-defined]
        self.model.command = f"{python} -c {shlex.quote(code)}"  # type: ignore[attr-defined]
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        port = self.model.server_address[1]
        env = {**os.environ, "FAKE_KEY": "x"}
        self.stderr = (tmp_path / "stderr.txt").open("wb")
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "karox6", "mcp",
                "--root", str(self.workspace), "--home", str(tmp_path / "home"),
                "--model", "m", "--provider", "openai-chat", "--no-sse", "--no-mcp",
                "--base-url", f"http://127.0.0.1:{port}/v1", "--api-key-env", "FAKE_KEY",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            env=env,
        )  # fmt: skip
        self.replies: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.child_pid: int | None = None

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if line.strip():
                self.replies.put(json.loads(line))

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self.replies.get(timeout=30)

    def start_command(self) -> int:
        """Initialize, start a run and wait until its long command is running."""
        self.call(1, "initialize", {"protocolVersion": "2025-06-18"})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        args = {"task": "go", "wait_seconds": 0}
        reply = self.call(2, "tools/call", {"name": "karox_run", "arguments": args})
        self.run_id = reply["result"]["structuredContent"]["run_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            text = self.pid_file.read_text() if self.pid_file.exists() else ""
            if text.strip():
                self.child_pid = int(text)
                return self.child_pid
            time.sleep(0.1)
        raise AssertionError("the command never started: " + self.stderr_text())

    def stderr_text(self) -> str:
        self.stderr.flush()
        return Path(self.stderr.name).read_text(encoding="utf-8", errors="replace")

    def wait_gone(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.child_pid is None or not _alive(self.child_pid):
                return True
            time.sleep(0.1)
        return False

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.child_pid is not None and _alive(self.child_pid):
            _kill(self.child_pid)
        self.model.shutdown()
        self.stderr.close()


@pytest.fixture
def server(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance = _Server(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def test_closing_stdin_during_a_long_poll_stops_the_command(server: _Server) -> None:
    pid = server.start_command()
    assert _alive(pid)
    # A long-poll is in flight when the client goes away (what clients do on exit).
    status = {"run_id": server.run_id, "wait_seconds": 50}
    server.send(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "karox_status", "arguments": status}}
    )  # fmt: skip
    time.sleep(0.3)
    started = time.monotonic()
    assert server.proc.stdin is not None
    server.proc.stdin.close()
    server.proc.wait(timeout=20)
    took = time.monotonic() - started
    assert took < 3.0, f"server took {took:.1f}s to exit; clients kill it after ~2-3s"
    assert server.wait_gone(5), "the command outlived the server"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")
def test_sigterm_stops_the_command(server: _Server) -> None:
    pid = server.start_command()
    assert _alive(pid)
    started = time.monotonic()
    server.proc.send_signal(signal.SIGTERM)  # stdin stays open, as with most clients
    server.proc.wait(timeout=20)
    took = time.monotonic() - started
    assert took < 5.0, f"server took {took:.1f}s to exit after SIGTERM"
    assert server.wait_gone(5), "the command outlived the server"
    assert server.proc.returncode == 128 + signal.SIGTERM
