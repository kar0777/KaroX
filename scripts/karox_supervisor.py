"""KaroX 4.0 supervisor + watchdog.

Runs the KaroX server as a child process, pings GET /watchdog/ping every
KAROX_SUPERVISOR_INTERVAL seconds (default 5), and restarts the whole process
tree in under 3 seconds after 2 consecutive failed heartbeats.

Usage (from repo root or anywhere):
    python scripts/karox_supervisor.py -- <server command ...>

Example:
    python scripts/karox_supervisor.py --port 8765 --api-key %KAROX_API_KEY% -- \
        python server/app_entry.py

If no explicit command is given after --, the supervisor falls back to
KAROX_SERVER_CMD from the environment.
A JSONL log is written next to the supervisor (or KAROX_SUPERVISOR_LOG).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

IS_WINDOWS = os.name == "nt"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Supervisor:
    def __init__(
        self,
        command: List[str],
        port: int,
        api_key: Optional[str],
        interval: float = 5.0,
        fail_threshold: int = 2,
        startup_grace: float = 20.0,
        log_path: Optional[str] = None,
    ) -> None:
        self.command = command
        self.port = port
        self.api_key = api_key
        self.interval = max(1.0, interval)
        self.fail_threshold = max(1, fail_threshold)
        self.startup_grace = max(3.0, startup_grace)
        self.proc: Optional[subprocess.Popen] = None
        self.restarts = 0
        self.stopping = False
        default_log = Path(os.environ.get("KAROX_SUPERVISOR_LOG") or (Path(__file__).parent / "karox_supervisor.jsonl"))
        self.log_path = Path(log_path) if log_path else default_log

    # -- logging ----------------------------------------------------------
    def log(self, event: str, **data: Any) -> None:
        record: Dict[str, Any] = {"ts": now_iso(), "event": event, **data}
        line = json.dumps(record, ensure_ascii=False)
        print(f"[supervisor] {line}", flush=True)
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # -- child lifecycle ---------------------------------------------------
    def start_child(self) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        self.proc = subprocess.Popen(self.command, env=env, creationflags=creationflags)
        self.log("child_started", pid=self.proc.pid, cmd=self.command, restarts=self.restarts)

    def kill_child_tree(self) -> None:
        if self.proc is None:
            return
        pid = self.proc.pid
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception as e:
            self.log("kill_error", pid=pid, error=str(e))
        finally:
            self.log("child_killed", pid=pid)
            self.proc = None

    # -- heartbeat ----------------------------------------------------------
    def ping(self) -> Optional[Dict[str, Any]]:
        for path in ("/watchdog/ping", "/health"):
            url = f"http://127.0.0.1:{self.port}{path}"
            headers = {"X-API-Key": self.api_key} if self.api_key else {}
            try:
                with urlopen(Request(url, headers=headers), timeout=3) as resp:
                    if resp.status == 200:
                        try:
                            return json.loads(resp.read().decode("utf-8", errors="replace"))
                        except json.JSONDecodeError:
                            return {"status": resp.status}
            except Exception:
                continue
        return None

    # -- main loop ----------------------------------------------------------
    def run(self) -> int:
        def handle_stop(_sig, _frame):
            self.stopping = True
            self.log("supervisor_stopping")
            self.kill_child_tree()
            sys.exit(0)

        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)

        self.start_child()
        deadline_ready = time.time() + self.startup_grace
        failures = 0
        while not self.stopping:
            time.sleep(self.interval)
            if self.proc is not None and self.proc.poll() is not None:
                self.log("child_exited", exitCode=self.proc.returncode)
                self.restart("child_exited")
                deadline_ready = time.time() + self.startup_grace
                failures = 0
                continue
            result = self.ping()
            if result is not None:
                failures = 0
                continue
            if time.time() < deadline_ready:
                continue  # still starting up
            failures += 1
            self.log("heartbeat_failed", consecutive=failures, threshold=self.fail_threshold)
            if failures >= self.fail_threshold:
                self.restart("heartbeat_timeout")
                deadline_ready = time.time() + self.startup_grace
                failures = 0
        return 0

    def restart(self, reason: str) -> None:
        started = time.time()
        self.kill_child_tree()
        self.restarts += 1
        self.start_child()
        self.log("child_restarted", reason=reason, downtimeSeconds=round(time.time() - started, 2), restarts=self.restarts)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KaroX supervisor + watchdog")
    parser.add_argument("--port", type=int, default=int(os.environ.get("KAROX_PORT", "8765")))
    parser.add_argument("--api-key", default=os.environ.get("KAROX_API_KEY"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("KAROX_SUPERVISOR_INTERVAL", "5")))
    parser.add_argument("--fail-threshold", type=int, default=int(os.environ.get("KAROX_SUPERVISOR_FAILS", "2")))
    parser.add_argument("--startup-grace", type=float, default=float(os.environ.get("KAROX_SUPERVISOR_GRACE", "20")))
    parser.add_argument("--log", default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Server command after --")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    command = [c for c in args.command if c != "--"]
    if not command:
        raw = os.environ.get("KAROX_SERVER_CMD", "")
        command = shlex.split(raw, posix=not IS_WINDOWS) if raw else []
    if not command:
        print("Не задана команда сервера: укажите её после -- или в KAROX_SERVER_CMD", file=sys.stderr)
        return 2
    supervisor = Supervisor(
        command=command,
        port=args.port,
        api_key=args.api_key,
        interval=args.interval,
        fail_threshold=args.fail_threshold,
        startup_grace=args.startup_grace,
        log_path=args.log,
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
