from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _path_setup
from karox.ellipsis_runtime import (
    ELLIPSIS_SYSTEM_INSTRUCTION,
    EllipsisAgentConfig,
    EllipsisAgentManager,
    EllipsisStateStore,
)
from karox.models import AccessProfile


class FakeSessions:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.revoked: list[str] = []

    def create(self, *args: object, **kwargs: object) -> object:
        self.events.append("local_session_created")
        return SimpleNamespace()

    def revoke(self, session_id: str) -> None:
        self.revoked.append(session_id)


class FakeCheckpoints:
    def create(self, *args: object, **kwargs: object) -> object:
        return SimpleNamespace(checkpoint_id="checkpoint-1")


class FakeLeases:
    def __init__(self) -> None:
        self.revoked: list[str] = []

    def mint(self, **kwargs: object) -> tuple[object, str]:
        return SimpleNamespace(expires_at=time.time() + 300), "opaque-credential-value"

    def revoke(self, name: str) -> None:
        self.revoked.append(name)


class FakeConnections:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, session_id: str, url: str) -> None:
        self.values[session_id] = url

    def delete(self, session_id: str) -> None:
        self.values.pop(session_id, None)


class FakeApi:
    def __init__(self) -> None:
        self.variables: dict[str, str] | None = None
        self.started: tuple[str, str] | None = None
        self.closed = False

    def create_draft(self, **kwargs: object) -> object:
        self.create_kwargs = kwargs
        return SimpleNamespace(session_id="ellipsis-session-1")

    def set_write_only_variables(
        self, session_id: str, variables: dict[str, str]
    ) -> None:
        self.variables = variables

    def start(self, session_id: str, task: str) -> None:
        self.started = (session_id, task)

    def stop(self, session_id: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


class EllipsisRuntimeTests(unittest.TestCase):
    def test_system_instruction_contains_no_connection_variables(self) -> None:
        self.assertIn("project is not present", ELLIPSIS_SYSTEM_INSTRUCTION.lower())
        self.assertIn("karox-remote", ELLIPSIS_SYSTEM_INSTRUCTION)
        self.assertNotIn("KAROX_REMOTE_URL", ELLIPSIS_SYSTEM_INSTRUCTION)
        self.assertNotIn("KAROX_REMOTE_CREDENTIAL", ELLIPSIS_SYSTEM_INSTRUCTION)
        self.assertNotIn("KAROX_SESSION_ID", ELLIPSIS_SYSTEM_INSTRUCTION)

    def test_start_creates_local_session_before_remote_preflight_and_persists_no_secret(self) -> None:
        with tempfile.TemporaryDirectory(prefix="karox-runtime-order-") as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            events: list[str] = []
            sessions = FakeSessions(events)
            leases = FakeLeases()
            connections = FakeConnections()
            state_store = EllipsisStateStore(root / "states")
            api = FakeApi()
            manager = EllipsisAgentManager(
                states=state_store,
                sessions=sessions,
                leases=leases,
                connections=connections,
                checkpoints=FakeCheckpoints(),
            )
            config = EllipsisAgentConfig(
                repository=repository,
                task="Edit the local file.",
                access_profile=AccessProfile.WORKSPACE_WRITE,
                tunnel="custom",
                public_url="https://private-bridge.invalid",
                command_commands=(("python", "-m", "unittest", "*"),),
                verification_commands=(("python", "-m", "unittest", "*"),),
                remote_install={"strategy": "preinstalled", "command": "karox-remote"},
            )

            def fake_preflight(value: EllipsisAgentConfig) -> dict[str, object]:
                events.append("remote_preflight")
                return {
                    "tools": [
                        "karox.repo.read_file",
                        "karox.repo.write_file",
                        "karox.report.get",
                    ]
                }

            popen_calls: list[tuple[object, ...]] = []

            def fake_popen(argv: object, *args: object, **kwargs: object) -> FakeProcess:
                popen_calls.append(tuple(argv))
                return FakeProcess(101 if len(popen_calls) == 1 else 202)

            with (
                patch.object(manager, "_local_repository", return_value=(repository, "main")),
                patch.object(manager, "preflight", side_effect=fake_preflight),
                patch.object(manager, "_api", return_value=api),
                patch.object(manager, "_wait_bridge", return_value=None),
                patch("karox.ellipsis_runtime.start_tunnel") as tunnel,
                patch("karox.ellipsis_runtime.subprocess.Popen", side_effect=fake_popen),
                patch("karox.ellipsis_runtime.runtime_dir", return_value=root / "runtime"),
            ):
                tunnel.return_value = SimpleNamespace(
                    public_url="https://private-bridge.invalid",
                    pid=None,
                    stop=lambda: None,
                )
                state = manager.start(config)

            self.assertEqual(events[:2], ["local_session_created", "remote_preflight"])
            self.assertEqual(state.bridge_pid, 101)
            self.assertEqual(state.watchdog_pid, 202)
            self.assertEqual(api.started, ("ellipsis-session-1", config.task))
            self.assertEqual(
                set(api.variables or {}),
                {
                    "KAROX_REMOTE_URL",
                    "KAROX_REMOTE_CREDENTIAL",
                    "KAROX_SESSION_ID",
                },
            )
            persisted = state_store.path(state.local_session_id).read_text(encoding="utf-8")
            self.assertNotIn("private-bridge.invalid", persisted)
            self.assertNotIn("opaque-credential-value", persisted)
            bridge_argv = json.dumps(popen_calls[0])
            self.assertNotIn("private-bridge.invalid", bridge_argv)
            self.assertNotIn("opaque-credential-value", bridge_argv)
            self.assertNotIn("ELLIPSIS_API_TOKEN", bridge_argv)
            self.assertTrue(api.closed)


if __name__ == "__main__":
    unittest.main()
