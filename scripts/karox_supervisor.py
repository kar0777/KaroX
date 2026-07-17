"""KaroX 4.1 supervisor + watchdog.

Runs the KaroX server as a child process, pings GET /watchdog/ping (falling
back to /health) every KAROX_SUPERVISOR_INTERVAL seconds (default 5), and
restarts the whole process tree in under 3 seconds after 2 consecutive failed
heartbeats.

Usage (from repo root or anywhere):
    python scripts/karox_supervisor.py [options] -- <server command ...>

Example:
    python scripts/karox_supervisor.py --port 8765 -- python -m uvicorn \
        --app-dir server repo_tools:app --host 127.0.0.1 --port 8765

The API key is taken from --api-key, KAROX_API_KEY, or REPO_TOOLS_API_KEY, so
launchers never have to put the secret on the command line. If no explicit
command is given after --, the supervisor falls back to KAROX_SERVER_CMD.
A JSONL log is written next to this script (or --log / KAROX_SUPERVISOR_LOG).
With --pid-file the current child PID is exported after every (re)start so
launchers can always stop the real server process even if the supervisor is
killed first.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import karox_ui as ui
except Exception:  # pretty output must never break the watchdog
    ui = None

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
        pid_file: Optional[str] = None,
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
        default_log = Path(
            os.environ.get("KAROX_SUPERVISOR_LOG")
            or (Path(__file__).parent / "karox_supervisor.jsonl")
        )
        self.log_path = Path(log_path) if log_path else default_log
        self.pid_file = Path(pid_file) if pid_file else None

    # -- logging -----------------------------------------------------------
    def log(self, event: str, **data: Any) -> None:
        record: Dict[str, Any] = {"ts": now_iso(), "event": event, **data}
        line = json.dumps(record, ensure_ascii=False)
        details = " \u00b7 ".join(f"{k}={v}" for k, v in data.items() if k != "cmd")
        pretty = f"watchdog \u00b7 {event}" + (f" \u00b7 {details}" if details else "")
        if ui:
            if event in ("child_started", "child_restarted"):
                ui.ok(pretty)
            elif "fail" in event or "exit" in event or "error" in event:
                ui.warn(pretty)
            else:
                ui.info(pretty)
        else:
            print(f"  \u25c6 {pretty}", flush=True)
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # -- child lifecycle ----------------------------------------------------
    def _write_pid_file(self, pid: Optional[int]) -> None:
        if self.pid_file is None:
            return
        try:
            if pid is None:
                self.pid_file.unlink(missing_ok=True)
            else:
                self.pid_file.parent.mkdir(parents=True, exist_ok=True)
                self.pid_file.write_text(str(pid), encoding="ascii")
        except OSError:
            pass

    def start_child(self) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        self.proc = subprocess.Popen(self.command, env=env, creationflags=creationflags)
        self._write_pid_file(self.proc.pid)
        self.log("child_started", pid=self.proc.pid, cmd=self.command, restarts=self.restarts)

    def kill_child_tree(self) -> None:
        if self.proc is None:
            return
        pid = self.proc.pid
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception as e:  # noqa: BLE001 - never die while killing
            self.log("kill_error", pid=pid, error=str(e))
        finally:
            self.log("child_killed", pid=pid)
            self.proc = None
            self._write_pid_file(None)

    def restart(self, reason: str) -> None:
        started = time.time()
        self.kill_child_tree()
        self.restarts += 1
        self.start_child()
        self.log(
            "child_restarted",
            reason=reason,
            restarts=self.restarts,
            downtimeSeconds=round(time.time() - started, 2),
        )

    # -- heartbeat -----------------------------------------------------------
    def ping(self) -> Optional[dict]:
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        for path in ("/watchdog/ping", "/health"):
            url = f"http://127.0.0.1:{self.port}{path}"
            try:
                req = Request(url, headers=headers)
                with urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
        return None

    # -- main loop ------------------------------------------------------------
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
        try:
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
        finally:
            self.kill_child_tree()
        return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KaroX server supervisor/watchdog")
    parser.add_argument("--port", type=int, default=int(os.environ.get("KAROX_PORT", "8765")))
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KAROX_API_KEY") or os.environ.get("REPO_TOOLS_API_KEY"),
    )
    parser.add_argument(
        "--interval", type=float, default=float(os.environ.get("KAROX_SUPERVISOR_INTERVAL", "5"))
    )
    parser.add_argument(
        "--fail-threshold", type=int, default=int(os.environ.get("KAROX_SUPERVISOR_FAILS", "2"))
    )
    parser.add_argument(
        "--startup-grace", type=float, default=float(os.environ.get("KAROX_SUPERVISOR_GRACE", "20"))
    )
    parser.add_argument("--log", default=os.environ.get("KAROX_SUPERVISOR_LOG"))
    parser.add_argument("--pid-file", default=os.environ.get("KAROX_SUPERVISOR_PID_FILE"))
    parser.add_argument("command", nargs=argparse.REMAINDER, help="server command after --")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        fallback = os.environ.get("KAROX_SERVER_CMD", "")
        command = shlex.split(fallback)
    if not command:
        print("No server command given. Pass it after -- or set KAROX_SERVER_CMD.", file=sys.stderr)
        return 2
    supervisor = Supervisor(
        command=command,
        port=args.port,
        api_key=args.api_key,
        interval=args.interval,
        fail_threshold=args.fail_threshold,
        startup_grace=args.startup_grace,
        log_path=args.log,
        pid_file=args.pid_file,
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
