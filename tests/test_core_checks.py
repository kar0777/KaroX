"""``checks.run``: what it will execute, how it is bounded, what it reports.

Evidence-based verification is only worth anything if the agent can reach the
command it needs to run, if a stopped check leaves nothing behind, if the output
it returns contains the diagnostic, and if the numbers in the answer are the
numbers that were actually used.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional, Sequence

from _support import cleanup_temporary_directory, initialize_git_repository
from karox.core import CoreError, CoreRuntime, InvalidCommand, VerificationRule
from karox.hosted_bridge import DEFAULT_HOSTED_DEADLINE_SECONDS
from karox.models import (
    AccessProfile,
    Capability,
    CoreCommand,
    Origin,
    OriginKind,
)
from karox.policy import CapabilityPolicy
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore


PYTEST_RULE = ("python", "-m", "pytest", "*")


def process_is_running(pid: int) -> bool:
    """True while ``pid`` still exists, without signalling it.

    ``os.kill(pid, 0)`` is not a liveness probe on Windows: CPython implements it
    with ``TerminateProcess``, so asking the question would answer it.
    """
    if sys.platform == "win32":
        # CSV keeps the PID column unambiguous; the default table format pads it
        # with whatever the image name and memory columns happen to contain.
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            # ``tasklist`` emits bytes in the OEM code page on localized
            # Windows; the default ANSI decoder raises UnicodeDecodeError in
            # the reader thread, which leaves ``stdout`` as ``None`` and turns
            # the ``in`` check below into a TypeError.  We only need the ASCII
            # pid token, so replace-mode decoding is exact.
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if completed.stdout is None:
            return False
        return f'"{pid}"' in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class VerificationRuleTests(unittest.TestCase):
    """The allowlist is the only real gate in front of a spawned process."""

    def test_an_entry_without_a_trailing_star_keeps_its_exact_argv_meaning(
        self,
    ) -> None:
        rule = VerificationRule.parse(["python", "-m", "pytest"])

        self.assertFalse(rule.wildcard)
        self.assertTrue(rule.matches(["python", "-m", "pytest"]))
        self.assertFalse(rule.matches(["python", "-m", "pytest", "tests"]))
        self.assertFalse(rule.matches(["python", "-m"]))

    def test_a_prefix_rule_admits_the_test_the_agent_just_wrote(self) -> None:
        rule = VerificationRule.parse(PYTEST_RULE)

        for argv in (
            ["python", "-m", "pytest"],
            ["python", "-m", "pytest", "tests/test_core.py::TestX", "-x"],
            ["python", "-m", "pytest", "--maxfail=1", "-q"],
            ["python", "-m", "pytest", "-k", "checks"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(rule.matches(argv))

    def test_a_prefix_rule_refuses_the_literal_star_it_was_written_with(
        self,
    ) -> None:
        rule = VerificationRule.parse(PYTEST_RULE)

        # Checks run without a shell, so a literal * reaches the child as a
        # filename that does not exist. Admitting it gave the model an approved
        # command that always failed, which reads as the allowlist being broken.
        self.assertFalse(rule.matches(["python", "-m", "pytest", "*"]))
        self.assertFalse(rule.matches(["python", "-m", "pytest", "tests", "*"]))

    def test_a_prefix_rule_refuses_another_executable_or_another_module(
        self,
    ) -> None:
        rule = VerificationRule.parse(PYTEST_RULE)

        for argv in (
            ["python3", "-m", "pytest", "tests"],
            ["/usr/bin/python", "-m", "pytest"],
            ["python", "-m", "pip", "install", "evil"],
            ["python", "-mpytest"],
            ["pytest"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(rule.matches(argv))

    def test_a_prefix_rule_refuses_code_execution_flags_in_the_tail(self) -> None:
        rule = VerificationRule.parse(PYTEST_RULE)

        for argv in (
            ["python", "-m", "pytest", "-c", "import os; os.system('whoami')"],
            ["python", "-m", "pytest", "-cimport os"],
            ["python", "-m", "pytest", "-Bc", "import os"],
            ["python", "-m", "pytest", "-e", "1"],
            ["python", "-m", "pytest", "--eval=1"],
            ["python", "-m", "pytest", "--exec", "whoami"],
            ["python", "-m", "pytest", "--command=whoami"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(rule.matches(argv))

    def test_a_rule_must_name_an_executable_and_star_must_come_last(self) -> None:
        with self.assertRaisesRegex(CoreError, "name an executable"):
            VerificationRule.parse(["*"])
        with self.assertRaisesRegex(CoreError, "last argument"):
            VerificationRule.parse(["python", "*", "pytest"])
        with self.assertRaisesRegex(CoreError, "non-empty strings"):
            VerificationRule.parse([])
        with self.assertRaisesRegex(CoreError, "non-empty strings"):
            VerificationRule.parse(["python", ""])


class CheckRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "verify sample",
            AccessProfile.WORKSPACE_WRITE,
            session_id="session",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.CHECKS_RUN,
                Capability.PROCESS_RUN,
                Capability.GIT_READ,
            },
        )

    def tearDown(self) -> None:
        # These tests kill process trees on purpose, and a killed child holds the
        # repository directory it was running in until Windows finishes tearing it
        # down -- which is a race this teardown used to lose.
        cleanup_temporary_directory(self.temporary)

    def runtime(self, *rules: Sequence[str]) -> CoreRuntime:
        return CoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
            verification_commands=list(rules) or None,
        )

    def check(
        self,
        runtime: CoreRuntime,
        argv: list[str],
        *,
        key: str,
        timeout_seconds: Optional[float] = None,
        deadline_seconds: float = 120.0,
    ):
        arguments: dict[str, object] = {"argv": argv}
        if timeout_seconds is not None:
            arguments["timeout_seconds"] = timeout_seconds
        command = CoreCommand(
            "checks.run",
            arguments,
            "session",
            self.origin,
            idempotency_key=key,
            deadline_seconds=deadline_seconds,
        )
        lease = self.sessions.acquire("session", "test")
        try:
            return runtime.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)

    # -- the approved set -------------------------------------------------

    def test_an_approved_prefix_runs_the_file_the_agent_named(self) -> None:
        runtime = self.runtime([sys.executable, "-m", "compileall", "-q", "*"])

        result = self.check(
            runtime,
            [sys.executable, "-m", "compileall", "-q", "sample.py"],
            key="compile-one-file",
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.data["verification_eligible"])

    def test_a_refused_command_leaves_no_pending_idempotency_intent(self) -> None:
        runtime = self.runtime([sys.executable, "-m", "compileall", "-q", "*"])

        for index, argv in enumerate(
            (
                [sys.executable, "-m", "compileall", "-q", "-c", "import os"],
                ["python3", "-m", "compileall", "-q", "sample.py"],
                [sys.executable, "-m", "pip", "install", "evil"],
            )
        ):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(InvalidCommand, "user-approved"):
                    self.check(runtime, argv, key=f"refused-{index}")
        self.assertEqual(self.sessions.load("session").idempotency, {})

    def test_the_approved_set_is_reported_with_its_wildcards(self) -> None:
        runtime = self.runtime(
            [sys.executable, "-m", "compileall", "-q", "*"],
            [sys.executable, "-m", "compileall", "-q", "src"],
        )

        # The native agent renders this set into its system prompt, so a rule
        # that admits a tail has to say so rather than look like an exact argv.
        self.assertEqual(
            runtime.verification_commands,
            frozenset(
                {
                    (sys.executable, "-m", "compileall", "-q", "*"),
                    (sys.executable, "-m", "compileall", "-q", "src"),
                }
            ),
        )

    def test_a_malformed_rule_is_refused_when_the_runtime_is_built(self) -> None:
        with self.assertRaisesRegex(CoreError, "name an executable"):
            self.runtime(["*"])

    # -- containment ------------------------------------------------------

    def test_a_timed_out_check_takes_its_descendants_with_it(self) -> None:
        runtime = self.runtime()
        worker = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)']); "
            "print(child.pid, flush=True); "
            "time.sleep(120)"
        )

        result = runtime._run([sys.executable, "-c", worker], 6.0)

        self.assertTrue(result["timed_out"])
        self.assertTrue(
            result["stdout"].strip(), "the worker never reported its own child"
        )
        pid = int(result["stdout"].strip())
        deadline = time.monotonic() + 30.0
        while process_is_running(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        # A worker that outlives its runner keeps the captured output handle and
        # whatever file locks it took, so the next check starts in a broken tree.
        self.assertFalse(process_is_running(pid))

    # -- returned output --------------------------------------------------

    def test_bounded_output_keeps_the_head_and_the_tail(self) -> None:
        runtime = self.runtime()
        runtime.MAX_OUTPUT_BYTES = 1_000
        # The raw buffer keeps the byte count identical on Windows, where a
        # text-mode newline would be translated on the way out.
        body = (
            "import sys; "
            "sys.stdout.buffer.write(b'ROOT-CAUSE\\n'); "
            "sys.stdout.buffer.write(b'x' * 40_000); "
            "sys.stdout.buffer.write(b'\\nSUMMARY\\n')"
        )
        expected = b"ROOT-CAUSE\n" + b"x" * 40_000 + b"\nSUMMARY\n"

        result = runtime._run([sys.executable, "-c", body], 30.0)

        self.assertTrue(result["stdout_truncated"])
        self.assertEqual(result["stdout_bytes"], len(expected))
        self.assertEqual(
            result["stdout_sha256"], hashlib.sha256(expected).hexdigest()
        )
        self.assertEqual(result["stdout_elided_bytes"], len(expected) - 1_000)
        self.assertTrue(result["stdout"].startswith("ROOT-CAUSE\n"))
        self.assertTrue(result["stdout"].endswith("\nSUMMARY\n"))
        self.assertIn(
            f"[karox: {len(expected) - 1_000} bytes elided", result["stdout"]
        )

    def test_output_that_fits_is_returned_whole(self) -> None:
        runtime = self.runtime()

        result = runtime._run(
            [sys.executable, "-c", "print('all of it')"], 30.0
        )

        self.assertFalse(result["stdout_truncated"])
        self.assertEqual(result["stdout_elided_bytes"], 0)
        self.assertEqual(result["stdout"].strip(), "all of it")
        self.assertNotIn("elided", result["stdout"])

    # -- reported timeouts ------------------------------------------------

    def test_a_clamped_timeout_reports_both_numbers_and_its_cause(self) -> None:
        runtime = self.runtime()

        result = self.check(
            runtime,
            [sys.executable, "-c", "print('quick')"],
            key="clamped",
            timeout_seconds=600.0,
            deadline_seconds=20.0,
        )

        self.assertEqual(result.data["requested_timeout"], 600.0)
        self.assertEqual(result.data["effective_timeout"], 20.0)
        self.assertEqual(result.data["timeout_clamped_by"], "request_deadline")

    def test_an_unclamped_timeout_says_nothing_clamped_it(self) -> None:
        runtime = self.runtime()

        result = self.check(
            runtime,
            [sys.executable, "-c", "print('quick')"],
            key="unclamped",
            timeout_seconds=10.0,
            deadline_seconds=120.0,
        )

        self.assertEqual(result.data["effective_timeout"], 10.0)
        self.assertIsNone(result.data["timeout_clamped_by"])

    def test_a_timeout_caused_by_a_clamp_explains_itself(self) -> None:
        runtime = self.runtime()

        result = self.check(
            runtime,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            key="clamped-timeout",
            timeout_seconds=600.0,
            deadline_seconds=1.0,
        )

        self.assertTrue(result.data["timed_out"])
        self.assertEqual(result.data["effective_timeout"], 1.0)
        self.assertIn("request_deadline", result.data["detail"])
        self.assertIn("600", result.data["detail"])
        recorded = self.sessions.load("session").checks[-1]
        self.assertEqual(recorded["effective_timeout"], 1.0)
        self.assertEqual(recorded["timeout_clamped_by"], "request_deadline")

    def test_hosted_deadlines_default_above_a_repository_test_suite(self) -> None:
        # A server-wide thirty seconds silently became every check's timeout.
        self.assertGreater(DEFAULT_HOSTED_DEADLINE_SECONDS, 30.0)
        self.assertGreater(
            inspect.signature(build_proxy_asgi_app)
            .parameters["deadline_seconds"]
            .default,
            30.0,
        )

    # -- no cached pass ---------------------------------------------------

    def test_a_repeated_key_reruns_the_check_instead_of_replaying_a_pass(
        self,
    ) -> None:
        runtime = self.runtime()
        counter = self.repository / "runs.txt"
        argv = [
            sys.executable,
            "-c",
            "import pathlib; p = pathlib.Path('runs.txt'); "
            "p.write_text(p.read_text() + 'x' if p.exists() else 'x')",
        ]

        first = self.check(runtime, argv, key="same-key")
        second = self.check(runtime, argv, key="same-key")

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(second.idempotent_replay)
        # A client that edits code and re-runs the same check must not be handed
        # the answer that described the previous working tree.
        self.assertEqual(counter.read_text(encoding="utf-8"), "xx")
        record = self.sessions.load("session")
        self.assertEqual(len(record.checks), 2)
        self.assertEqual(len(record.evidence), 2)


class TemporaryDirectoryCleanupTests(unittest.TestCase):
    """The teardown helper that stops a killed child from failing the suite.

    These tests deliberately kill process trees, and on Windows the directory a
    killed child was running in stays open until the OS finishes tearing the
    process down. Teardown lost that race often enough to be recorded as a known
    flake in IMPLEMENTATION_STATUS.
    """

    def test_cleanup_retries_a_handle_that_clears(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        attempts: list[int] = []
        real_cleanup = temporary.cleanup

        def flaky_cleanup() -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError(13, "The process cannot access the file")
            real_cleanup()

        temporary.cleanup = flaky_cleanup  # type: ignore[method-assign]
        cleanup_temporary_directory(temporary)

        self.assertGreaterEqual(len(attempts), 2, "the cleanup was not retried")
        self.assertFalse(Path(temporary.name).exists())

    def test_cleanup_still_raises_when_the_handle_never_clears(self) -> None:
        """Retrying must not become ignoring: a real leak still has to be loud."""
        temporary = tempfile.TemporaryDirectory()
        try:
            def always_locked() -> None:
                raise PermissionError(13, "The process cannot access the file")

            temporary.cleanup = always_locked  # type: ignore[method-assign]
            with self.assertRaises(PermissionError):
                cleanup_temporary_directory(temporary, timeout=0.2)
        finally:
            tempfile.TemporaryDirectory.cleanup(temporary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
