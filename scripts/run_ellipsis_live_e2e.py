"""Strictly gated live acceptance test for Ellipsis Opus 5 + local KaroX.

This script is never called by the normal test suite. It creates a temporary,
unpublished local Git repository, starts a real Ellipsis interactive session
without repository metadata, and verifies that all evidence appears locally.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

from karox.ellipsis_runtime import (
    EllipsisAgentConfig,
    EllipsisAgentManager,
    EllipsisRuntimeError,
)
from karox.models import AccessProfile
from karox.remote_lease import EllipsisLeaseError


CONFIRMATION = "LIVE ELLIPSIS"
TERMINAL_STATES = {"completed", "complete", "succeeded", "success", "failed", "stopped"}


def _run(repository: Path, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv),
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"local command failed with exit code {completed.returncode}: {' '.join(argv)}"
        )
    return completed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_ellipsis_live_e2e")
    parser.add_argument("--allow-live-ellipsis", action="store_true")
    parser.add_argument("--budget", type=float, default=0.10)
    parser.add_argument("--tunnel", choices=["tailscale", "cloudflare"], default="tailscale")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--confirmation")
    return parser


def _confirm(args: argparse.Namespace) -> None:
    if not args.allow_live_ellipsis:
        raise SystemExit("live test requires --allow-live-ellipsis")
    if not os.environ.get("ELLIPSIS_API_TOKEN", "").strip():
        raise SystemExit("live test requires ELLIPSIS_API_TOKEN")
    if not os.environ.get("ELLIPSIS_API_BASE_URL", "").strip():
        raise SystemExit("live test requires ELLIPSIS_API_BASE_URL")
    if not 0 < args.budget <= 0.10:
        raise SystemExit("live test budget must be greater than 0 and at most 0.10 USD")
    supplied = args.confirmation
    if supplied is None and sys.stdin.isatty():
        supplied = input(f"Type {CONFIRMATION!r} to spend up to {args.budget:.2f} USD: ")
    if supplied != CONFIRMATION:
        raise SystemExit("live test requires the exact explicit confirmation")


def _create_repository(root: Path) -> Path:
    repository = root / "private-local-repository"
    repository.mkdir()
    _run(repository, ["git", "init"])
    _run(repository, ["git", "config", "user.email", "karox-live@example.invalid"])
    _run(repository, ["git", "config", "user.name", "KaroX Live Test"])
    (repository / "app.py").write_text(
        "def answer():\n    return 1\n",
        encoding="utf-8",
    )
    (repository / "test_app.py").write_text(
        "import unittest\n"
        "from app import answer\n\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_answer(self):\n"
        "        self.assertEqual(answer(), 2)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    _run(repository, ["git", "add", "app.py", "test_app.py"])
    _run(repository, ["git", "commit", "-m", "initial local fixture"])
    if _run(repository, ["git", "remote", "-v"]).stdout.strip():
        raise RuntimeError("live fixture unexpectedly has a Git remote")
    return repository


def run_live(args: argparse.Namespace) -> int:
    _confirm(args)
    with tempfile.TemporaryDirectory(prefix="karox-ellipsis-live-") as temporary:
        repository = _create_repository(Path(temporary))
        manager = EllipsisAgentManager()
        state = manager.start(
            EllipsisAgentConfig(
                repository=repository,
                task=(
                    "Use only karox-remote. Run preflight, read app.py, change answer() "
                    "to return 2, run `python -m unittest -v`, inspect the local Git diff, "
                    "and report verified evidence. Do not clone or create any repository."
                ),
                access_profile=AccessProfile.WORKSPACE_WRITE,
                budget=args.budget,
                currency="USD",
                tunnel=args.tunnel,
                port=args.port,
                ttl_seconds=min(max(args.timeout_seconds + 120.0, 300.0), 3600.0),
                verification_commands=(("python", "-m", "unittest", "*"),),
                command_commands=(("python", "-m", "unittest", "*"),),
                allow_commit=False,
            )
        )
        try:
            deadline = time.monotonic() + args.timeout_seconds
            while time.monotonic() < deadline:
                manager.poll_events(state)
                refreshed, _ = manager.status(state.local_session_id)
                state = refreshed
                if state.cost is not None and state.cost > args.budget + 1e-9:
                    raise RuntimeError("Ellipsis reported cost above the configured budget")
                if state.status.lower() in TERMINAL_STATES:
                    break
                time.sleep(2.0)
            else:
                raise RuntimeError("live Ellipsis session did not finish before timeout")

            content = (repository / "app.py").read_text(encoding="utf-8")
            if "return 2" not in content:
                raise RuntimeError("the expected change did not appear in the local file")
            _run(repository, [sys.executable, "-m", "unittest", "-v"])
            diff = _run(repository, ["git", "diff", "--", "app.py"]).stdout
            if "return 2" not in diff:
                raise RuntimeError("the expected local Git diff was not produced")
            if _run(repository, ["git", "remote", "-v"]).stdout.strip():
                raise RuntimeError("the unpublished fixture acquired a Git remote")
            report = manager.report(state.local_session_id)
            print("Local file changed: yes")
            print("Local test passed: yes")
            print("Local Git diff present: yes")
            print("Git remotes/clones supplied to Ellipsis: none")
            print(f"KaroX report returned: {bool(report)}")
        finally:
            stopped = manager.stop(state.local_session_id)
            try:
                manager.leases.validate(
                    stopped.credential_name,
                    local_session_id=stopped.local_session_id,
                    ellipsis_session_id=stopped.ellipsis_session_id,
                    repository=repository,
                    access_profile=stopped.access_profile,
                )
            except EllipsisLeaseError:
                print("Credential revoked: yes")
            else:
                raise RuntimeError("credential remained valid after stop")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        return run_live(args)
    except (EllipsisRuntimeError, RuntimeError, OSError) as exc:
        print(f"live Ellipsis E2E failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
