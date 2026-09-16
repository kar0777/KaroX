"""Short smoke for the real isolated long-job acceptance orchestrator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _support import ROOT, SRC  # noqa: F401


class IsolatedLongJobAcceptanceSmokeTests(unittest.TestCase):
    def test_isolated_bridge_survives_managed_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / "result.json"
            script = ROOT / "scripts" / "long_job_bridge_acceptance.py"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--wmi-launch",
                    str(result),
                    "--duration",
                    "12",
                    "--probe-after",
                    "3",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=40,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(completed.stdout.strip(), "launcher returned no orchestrator PID")
            deadline = time.monotonic() + 60
            payload = None
            while time.monotonic() < deadline:
                if result.exists():
                    try:
                        payload = json.loads(result.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, dict) and payload.get("status") in {"passed", "failed"}:
                        break
                time.sleep(0.25)
            self.assertIsInstance(payload, dict)
            assert isinstance(payload, dict)
            error_detail = (payload.get("error") or "") + "\n" + (payload.get("error_traceback") or "")
            self.assertEqual(payload.get("status"), "passed", error_detail)
            self.assertTrue(payload.get("same_bridge_pid"))
            self.assertTrue(payload.get("bridge_alive_after_job"))
            self.assertTrue(payload.get("threshold_probe_completed"))
            self.assertTrue(payload["client_disconnect"]["bridge_alive"])
            self.assertTrue(payload["client_disconnect"]["fresh_client_succeeded"])
            self.assertEqual(payload["client_disconnect"]["job_status"], "running")
            self.assertTrue(payload["restart_recovery"]["new_process"])
            self.assertEqual(payload["restart_recovery"]["status"], "passed")
            self.assertTrue(payload["restart_recovery"]["logs_available"])
            self.assertTrue(payload["restart_recovery"]["runtime_ok"])
            self.assertLess(payload["job"]["start_latency_seconds"], 5.0)
            self.assertEqual(payload["job"]["final"]["status"], "passed")
            self.assertEqual(payload["isolated"]["tunnel"], "none")
            if os.name == "nt":
                self.assertGreater(int(completed.stdout.strip().splitlines()[-1]), 0)

    def test_launch_manual_over_210_second_acceptance(self) -> None:
        trigger = ROOT / "scratch" / "run_long_job_bridge_acceptance.json"
        try:
            request = json.loads(trigger.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.skipTest("manual long-job trigger is absent")
        if request.get("mode") != "launch":
            self.skipTest("manual long-job trigger is not armed")
        result = ROOT / str(request["result"])
        if request.get("kind") == "full_suite":
            script = ROOT / "scripts" / "full_suite_managed_acceptance.py"
            launch_argv = [
                sys.executable,
                str(script),
                "--wmi-launch",
                str(result),
                "--repository",
                str(ROOT),
            ]
        else:
            script = ROOT / "scripts" / "long_job_bridge_acceptance.py"
            launch_argv = [
                sys.executable,
                str(script),
                "--wmi-launch",
                str(result),
                "--duration",
                str(float(request.get("duration", 230.0))),
                "--probe-after",
                str(float(request.get("probe_after", 212.0))),
            ]
        completed = subprocess.run(
            launch_argv,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        orchestrator_pid = int(completed.stdout.strip().splitlines()[-1])
        trigger.write_text(
            json.dumps(
                {
                    **request,
                    "mode": "launched",
                    "orchestrator_pid": orchestrator_pid,
                    "launched_at": time.time(),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertGreater(orchestrator_pid, 0)


if __name__ == "__main__":
    unittest.main()
