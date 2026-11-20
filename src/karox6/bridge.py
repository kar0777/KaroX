"""The chat's `/bridge`: run `karox mcp --http --tunnel tailscale --supervise` beside the
chat, so ChatGPT (web), Claude and other hosted clients can use KaroX on this machine.

The server runs as a child process (its own process group, so it can be stopped politely
and remove its Tailscale route); its output goes to <home>/mcp/bridge.log, from which the
connector URL is read. The bridge stops with the chat.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from karox6.lifetime import bind_to_karox, join_karox_job

URL_RE = re.compile(r"Connector URL \(([^)]*)\): (\S+)")
START_TIMEOUT_S = 120.0
STOP_TIMEOUT_S = 15.0


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.log_path = home / "mcp" / "bridge.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.url: str | None = None
        self.where = ""
        self.mode = ""
        self.root: Path | None = None
        self.launcher = [sys.executable, "-m", "karox6"]  # tests swap in a stand-in

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(
        self,
        *,
        mode: str,
        root: Path,
        agent_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        public: bool = True,
    ) -> str:
        """Start the server and wait for its connector URL. Blocking: call it in a thread."""
        if self.running():
            raise BridgeError("the bridge is already running; /bridge stop first")
        command = [
            *self.launcher,
            "mcp",
            "--http",
            "127.0.0.1:0",  # any free port: the connector URL does not include it
            "--root",
            str(root),
            "--home",
            str(self.home),
            "--tunnel",
            "tailscale" if public else "tailnet",
            "--supervise",
        ]
        command += ["--direct"] if mode == "direct" else list(agent_args)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        child_env = {**os.environ, **(env or {})}
        joined = join_karox_job()
        with self.log_path.open("wb") as log:
            windows = sys.platform == "win32"
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=child_env,
                # its own group: CTRL_BREAK (Windows) / SIGTERM reach it alone
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if windows else 0,
                start_new_session=not windows,
            )
        if not joined:
            bind_to_karox(self.process.pid)
        self.mode, self.root, self.url = mode, root, None
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            found = URL_RE.search(self._log())
            if found:
                self.where, self.url = found.group(1), found.group(2)
                return self.url
            if self.process.poll() is not None:
                break
            time.sleep(0.5)
        tail = self.log_tail()
        self.stop()
        raise BridgeError(
            f"the bridge did not start:\n{tail}" if tail else "the bridge did not start"
        )

    def stop(self) -> None:
        """Ask the server to stop (it removes its Tailscale route); kill it if it hangs."""
        process, self.process, self.url = self.process, None, None
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            process.wait(5)

    def log_tail(self, lines: int = 8) -> str:
        text = self._log().strip().splitlines()
        return "\n".join(text[-lines:])

    def _log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
