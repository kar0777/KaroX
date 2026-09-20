"""Starting a child that outlives its launcher, on a Windows that forbids it.

Both durable KaroX spawn sites -- a saved bridge's owner and its supervisor --
go through ``spawn_detached``. The behaviour under test is the one that made
unattended revival work at all: ``CREATE_BREAKAWAY_FROM_JOB`` is an *attempt*,
because a caller inside a job created without ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``
-- which is every Task Scheduler action -- gets ``WinError 5`` from
``CreateProcess`` rather than a silently ignored flag. Before the fallback the
five-minute watchdog could start nothing and reported nothing but
``Last Result: 1``.

The flag arithmetic is asserted with ``os.name`` patched, so the same
expectations hold when the suite runs on a machine that is not Windows.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from _support import SRC  # noqa: F401

from karox import detached_process as dp


def _job_denied() -> OSError:
    """What ``CreateProcess`` raises for a breakaway a job does not permit."""
    exc = OSError("Access is denied")
    exc.winerror = 5
    return exc


class DetachedFlagTests(unittest.TestCase):
    def test_windows_asks_for_a_windowless_child_of_its_own_group(self) -> None:
        with patch.object(dp.os, "name", "nt"):
            flags = dp.detached_flags()
        self.assertFalse(flags & dp.DETACHED_PROCESS)
        self.assertTrue(flags & dp.CREATE_NO_WINDOW)
        self.assertTrue(flags & dp.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(flags & dp.CREATE_BREAKAWAY_FROM_JOB)

    def test_the_fallback_gives_up_the_breakaway_bit_and_nothing_else(self) -> None:
        with patch.object(dp.os, "name", "nt"):
            full = dp.detached_flags()
            fallback = dp.detached_flags(breakaway=False)
        self.assertEqual(full ^ fallback, dp.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(fallback & dp.CREATE_BREAKAWAY_FROM_JOB)

    def test_a_posix_child_carries_no_creation_flags(self) -> None:
        with patch.object(dp.os, "name", "posix"):
            self.assertEqual(dp.detached_flags(), 0)
            self.assertEqual(dp.detached_flags(breakaway=False), 0)


class SpawnDetachedTests(unittest.TestCase):
    """``spawn_detached`` on a patched ``Popen``; no child is ever created."""

    def _spawn(
        self,
        popen: MagicMock,
        *,
        name: str = "nt",
        cwd: str | None = None,
    ) -> tuple[object, str]:
        with patch.object(dp.os, "name", name), patch.object(
            dp.subprocess, "Popen", popen
        ):
            return dp.spawn_detached(["python", "-m", "karox.saved_bridge_supervisor"], cwd=cwd)

    def test_breakaway_is_the_first_thing_tried(self) -> None:
        popen = MagicMock(return_value="child")
        process, mechanism = self._spawn(popen)
        self.assertEqual(process, "child")
        self.assertEqual(mechanism, "breakaway")
        self.assertEqual(popen.call_count, 1)
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & dp.CREATE_BREAKAWAY_FROM_JOB)

    def test_a_job_that_forbids_breakaway_still_gets_a_child(self) -> None:
        popen = MagicMock(side_effect=[_job_denied(), "child"])
        process, mechanism = self._spawn(popen)
        self.assertEqual(process, "child")
        self.assertEqual(popen.call_count, 2)
        second = popen.call_args_list[1].kwargs["creationflags"]
        self.assertFalse(second & dp.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(second & dp.DETACHED_PROCESS)
        self.assertTrue(second & dp.CREATE_NO_WINDOW)
        self.assertTrue(second & dp.CREATE_NEW_PROCESS_GROUP)

    def test_the_refused_attempt_is_named_so_a_state_file_can_record_it(self) -> None:
        popen = MagicMock(side_effect=[_job_denied(), "child"])
        _, mechanism = self._spawn(popen)
        self.assertEqual(mechanism, "in_caller_job after breakaway:5")

    def test_when_nothing_starts_the_caller_learns_every_error(self) -> None:
        popen = MagicMock(side_effect=[_job_denied(), OSError("boom")])
        process, mechanism = self._spawn(popen)
        self.assertIsNone(process)
        self.assertTrue(mechanism.startswith("spawn_failed:"), mechanism)
        self.assertIn("breakaway:5", mechanism)
        self.assertIn("in_caller_job:OSError", mechanism)

    def test_the_child_holds_none_of_the_callers_console(self) -> None:
        popen = MagicMock(return_value="child")
        self._spawn(popen)
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_the_repository_is_handed_to_the_child_as_its_directory(self) -> None:
        popen = MagicMock(return_value="child")
        self._spawn(popen, cwd=r"D:\проекты\KaroX-v5")
        self.assertEqual(popen.call_args.kwargs["cwd"], r"D:\проекты\KaroX-v5")

    def test_posix_detaches_with_a_session_and_tries_once(self) -> None:
        popen = MagicMock(return_value="child")
        process, mechanism = self._spawn(popen, name="posix")
        self.assertEqual(process, "child")
        self.assertEqual(mechanism, "breakaway")
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_a_posix_failure_is_not_retried_under_a_second_name(self) -> None:
        popen = MagicMock(side_effect=OSError("no such file"))
        process, mechanism = self._spawn(popen, name="posix")
        self.assertIsNone(process)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(mechanism, "spawn_failed:breakaway:OSError")


if __name__ == "__main__":
    unittest.main()
