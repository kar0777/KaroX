from __future__ import annotations

"""Regression coverage for spacing failed local bridge canary probes.

This deliberately drives the real ``run_web_bridge`` owner loop with a fake
monotonic clock.  It is not a unit test of the scheduling variable: the probe,
confirmation counter, recycle, and replacement all run through the production
supervisor loop while process, tunnel, and filesystem side effects stay mocked.
"""

import io
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from _support import SRC  # noqa: F401 - inserts candidate src on sys.path
from karox.models import AccessProfile
from karox.web_bridge_launcher import WebBridgeConnectConfig, run_web_bridge


class LocalCanarySpacingTests(unittest.TestCase):
    def test_failed_canaries_are_spaced_and_only_later_persistent_failure_recycles(
        self,
    ) -> None:
        """A miss waits an interval; a later pair of misses still recycles the child.

        Tick pairs are ``(owner-loop time, local-canary time)``.  The adjacent
        ticks at 0 and 1 model the formerly-dangerous tight loop.  The healthy
        probe at 10 clears the first miss; the failures at 20 and 30 are a new
        persistent failure and must therefore recycle only at 30.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()

            bridge = MagicMock()
            bridge.pid = 5201
            bridge.stdout = MagicMock()
            bridge_state = {"exited": False}
            bridge.poll.side_effect = lambda: 1 if bridge_state["exited"] else None

            recovered_bridge = MagicMock()
            recovered_bridge.pid = 5202
            recovered_bridge.poll.return_value = None
            recovered_bridge.stdout = MagicMock()

            clock = {"now": None}
            # A supervisor may read monotonic more often than this scenario needs
            # (for example, while shutting down the replacement child).  Keep the
            # fake clock deterministic rather than letting an incidental extra read
            # turn the regression test into StopIteration.
            post_script_time = 31.0
            monotonic_values = iter(
                [
                    0.0,
                    0.0,  # first failed canary
                    1.0,
                    1.0,  # adjacent loop tick: no canary due
                    10.0,
                    10.0,  # next interval: recovery clears the failure
                    20.0,
                    20.0,  # later failure, first confirmation
                    21.0,
                    21.0,  # adjacent loop tick: still no canary due
                    30.0,
                    30.0,  # later interval: persistent failure recycles
                    post_script_time,  # replacement-child loop tick
                ]
            )

            def monotonic() -> float:
                value = next(monotonic_values, post_script_time)
                clock["now"] = value
                return value

            probe_times: list[float] = []
            probe_results = iter([False, True, False, False])

            def local_probe(*_args: object, **_kwargs: object) -> bool:
                assert isinstance(clock["now"], float)
                probe_times.append(clock["now"])
                return next(probe_results)

            recycle_times: list[float] = []

            def terminate_bridge() -> None:
                recycle_times.append(clock["now"])
                bridge_state["exited"] = True

            bridge.terminate.side_effect = terminate_bridge

            sleeps = {"count": 0}
            max_sleep_calls = 12

            def sleep(_: float) -> None:
                sleeps["count"] += 1
                # Exit only after the expected replacement has been spawned and the
                # full canary/recycle sequence has occurred; do not couple shutdown
                # to an incidental owner-loop iteration count.
                if (
                    popen.call_count == 2
                    and probe_times == [0.0, 10.0, 20.0, 30.0]
                    and recycle_times == [30.0]
                ):
                    raise KeyboardInterrupt
                if sleeps["count"] >= max_sleep_calls:
                    raise AssertionError("owner loop did not reach the expected terminal state")

            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            mirrored = MagicMock()
            mirrored.detail.return_value = ""
            mirrored.reader = MagicMock()

            with ExitStack() as stack:
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "KAROX_RUNTIME_DIR": str(root),
                            "KAROX_VNEXT_RUNTIME_DIR": str(root),
                        },
                    )
                )
                stack.enter_context(
                    patch("karox.web_bridge_launcher._port_is_available", return_value=True)
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher.reap_orphaned_web_bridges",
                        return_value=[],
                    )
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher._try_acquire_saved_bridge_owner_lock",
                        return_value=object(),
                    )
                )
                stack.enter_context(
                    patch("karox.web_bridge_launcher._release_saved_bridge_owner_lock")
                )
                stack.enter_context(
                    patch("karox.web_bridge_launcher._create_child_job", return_value=None)
                )
                stack.enter_context(patch("karox.web_bridge_launcher._adopt_child"))
                stack.enter_context(
                    patch("karox.web_bridge_launcher._consume_stop_request", return_value=None)
                )
                stack.enter_context(
                    patch("karox.web_bridge_launcher.watchdog_dir", return_value=root)
                )
                stack.enter_context(patch("karox.web_bridge_launcher.claim_watchdog"))
                stack.enter_context(patch("karox.web_bridge_launcher.write_watchdog"))
                stack.enter_context(
                    patch("karox.web_bridge_launcher._release_own_watchdog")
                )
                stack.enter_context(patch("karox.web_bridge_launcher._record_owner_exit"))
                stack.enter_context(patch("karox.web_bridge_launcher._stop_process"))
                stack.enter_context(
                    patch("karox.web_bridge_launcher.SessionStore", return_value=sessions)
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher.BridgeCredentialStore",
                        return_value=credentials,
                    )
                )
                stack.enter_context(
                    patch(
                        "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                        return_value=6201,
                    )
                )
                popen = stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher.subprocess.Popen",
                        side_effect=[bridge, recovered_bridge],
                    )
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher._mirror_child_output",
                        return_value=mirrored,
                    )
                )
                stack.enter_context(patch("karox.web_bridge_launcher._wait_for_bridge"))
                stack.enter_context(
                    patch("karox.web_bridge_launcher._wait_for_local_port_release")
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher.local_mcp_route_healthy",
                        side_effect=local_probe,
                    )
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher.time.monotonic", side_effect=monotonic
                    )
                )
                stack.enter_context(
                    patch("karox.web_bridge_launcher.time.sleep", side_effect=sleep)
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher.web_bridge_diagnostics",
                        return_value={
                            "url_stability": "stable",
                            "deadline_advisory": None,
                        },
                    )
                )
                stack.enter_context(
                    patch(
                        "karox.web_bridge_launcher.web_bridge_connection_instructions",
                        return_value=[],
                    )
                )
                stack.enter_context(
                    patch("karox.web_bridge_launcher.ephemeral_url_warning", return_value=None)
                )
                stack.enter_context(
                    patch("karox.web_bridge_launcher.known_bridge_profiles", return_value=[])
                )
                with redirect_stdout(io.StringIO()):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="chatgpt-web",
                            repository=repository,
                            access_profile=AccessProfile.WORKSPACE_WRITE,
                            tunnel="custom",
                            public_url="https://bridge.example.test",
                            saved_profile_name="spacing-regression",
                            local_health_interval_seconds=10.0,
                        )
                    )

        self.assertEqual(code, 0)
        # No second sample occurred on the immediately adjacent ticks (1 and 21).
        self.assertEqual(probe_times, [0.0, 10.0, 20.0, 30.0])
        # The recovery at 10 reset the first failure: the miss at 20 alone did not
        # recycle.  Only the later consecutive miss at 30 recycled the original.
        self.assertEqual(recycle_times, [30.0])
        bridge.terminate.assert_called_once()
        self.assertEqual(popen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
