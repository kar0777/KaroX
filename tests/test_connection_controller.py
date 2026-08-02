"""Contract tests for the unified saved-connection controller."""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401

from karox.connection_controller import ConnectionController
from karox.connection_runtime import ConnectionRuntimeError
from karox.connections import ConnectionError, McpClientTarget


def _target(
    connection_id: str = "c-0123456789abcdef",
    *,
    public_url: str | None = "https://bridge.example.com",
    tunnel: str = "cloudflare",
) -> McpClientTarget:
    return McpClientTarget(
        connection_id=connection_id,
        name="ClickUp",
        preset_id="clickup",
        transport="streamable_http",
        endpoint_path="/mcp",
        auth_scheme="bearer",
        tunnel=tunnel,
        runtime_profile="generic-streamable-http",
        public_url=public_url,
        credential_ref=f"os-keyring:connection/{connection_id}",
        credential_fingerprint="sha256:fixture",
        port=8765,
    )


class _Registry:
    def __init__(self, *targets: McpClientTarget) -> None:
        self.targets = {target.connection_id: target for target in targets}

    def list(self):
        return sorted(self.targets.values(), key=lambda item: item.name)

    def get(self, connection_id: str):
        try:
            return self.targets[connection_id]
        except KeyError as exc:
            raise ConnectionError(f"connection does not exist: {connection_id}") from exc

    def remove(self, connection_id: str):
        return self.targets.pop(connection_id)


class _RuntimeManager:
    def __init__(self, states: dict[str, str]) -> None:
        self.states = dict(states)
        self.stopped: list[str] = []
        self.forgotten: list[str] = []

    def status(self, connection_id: str):
        return {
            "connection_id": connection_id,
            "runtime_id": f"rt-{connection_id}",
            "state": self.states.get(connection_id, "configured_not_running"),
            "managed": self.states.get(connection_id) in {"running", "degraded"},
        }

    def stop(self, connection_id: str):
        self.stopped.append(connection_id)
        self.states[connection_id] = "stopped"
        return self.status(connection_id)

    def forget(self, connection_id: str):
        self.forgotten.append(connection_id)
        self.states.pop(connection_id, None)


class ConnectionControllerTests(unittest.TestCase):
    def _controller(
        self,
        target: McpClientTarget,
        *,
        state: str = "configured_not_running",
    ) -> tuple[ConnectionController, _Registry, _RuntimeManager, dict[str, object]]:
        registry = _Registry(target)
        runtime = _RuntimeManager({target.connection_id: state})
        seen: dict[str, object] = {}

        def tester(target_arg, *, endpoint_url, secret, timeout_seconds):
            seen.update(
                tested=target_arg.connection_id,
                endpoint=endpoint_url,
                secret=secret,
                timeout=timeout_seconds,
            )
            return {"state": "ok", "tool_count": 3}

        def remover(connection_id, *, registry, credentials=None):
            seen["credentials"] = credentials
            return registry.remove(connection_id)

        def launcher(target_arg):
            seen["launched"] = target_arg.connection_id
            runtime.states[target_arg.connection_id] = "running"
            return {
                "success": True,
                "target": target_arg,
                "public_endpoint": target_arg.effective_url,
                "local_endpoint": f"http://127.0.0.1:{target_arg.port}/mcp",
                "runtime_id": f"rt-{target_arg.connection_id}",
            }

        controller = ConnectionController(
            registry=registry,
            runtime_manager=runtime,
            tester=tester,
            secret_resolver=lambda target_arg: f"secret-for-{target_arg.connection_id}",
            remover=remover,
            launcher=launcher,
            credentials=object(),
        )
        return controller, registry, runtime, seen

    def test_list_combines_saved_configuration_and_runtime_state(self) -> None:
        target = _target()
        controller, _, _, seen = self._controller(target, state="running")
        rows = controller.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target, target)
        self.assertEqual(rows[0].state, "running")
        self.assertEqual(rows[0].endpoint, "https://bridge.example.com/mcp")
        support = controller.launch_support(target.connection_id)
        self.assertTrue(support.supported)
        self.assertEqual(support.launcher_id, "saved-clickup")
        already = controller.start(target.connection_id)
        self.assertEqual(already.status, "already_running")
        self.assertNotIn("launched", seen)

    def test_test_uses_the_same_endpoint_and_secret_dispatch_for_every_surface(self) -> None:
        target = _target()
        controller, _, _, seen = self._controller(target)
        result = controller.test(target.connection_id, timeout_seconds=7.5)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["url"], "https://bridge.example.com/mcp")
        self.assertEqual(seen["endpoint"], result["url"])
        self.assertEqual(seen["secret"], f"secret-for-{target.connection_id}")
        self.assertEqual(seen["timeout"], 7.5)
        started = controller.start(target.connection_id)
        self.assertTrue(started.success)
        self.assertEqual(started.status, "started")
        self.assertEqual(started.runtime["state"], "running")
        self.assertEqual(seen["launched"], target.connection_id)

    def test_test_requires_a_known_endpoint(self) -> None:
        target = _target(public_url=None)
        controller, _, _, _ = self._controller(target)
        with self.assertRaisesRegex(ConnectionError, "no endpoint URL"):
            controller.test(target.connection_id)

    def test_endpoint_override_allows_a_configured_connection_probe(self) -> None:
        target = _target(public_url=None)
        controller, _, runtime, seen = self._controller(target)
        result = controller.test(
            target.connection_id,
            endpoint_url="https://override.example.com/mcp",
        )
        self.assertEqual(result["url"], "https://override.example.com/mcp")
        self.assertEqual(seen["endpoint"], result["url"])
        runtime.states[target.connection_id] = "running"
        restarted = controller.restart(target.connection_id)
        self.assertEqual(restarted.status, "restarted")
        self.assertTrue(restarted.stopped_previous)
        self.assertEqual(runtime.stopped, [target.connection_id])
        self.assertEqual(seen["launched"], target.connection_id)

    def test_remove_stops_a_managed_runtime_then_removes_and_forgets(self) -> None:
        target = _target()
        controller, registry, runtime, seen = self._controller(target, state="running")
        result = controller.remove(target.connection_id)
        self.assertEqual(result["status"], "removed")
        self.assertEqual(runtime.stopped, [target.connection_id])
        self.assertEqual(runtime.forgotten, [target.connection_id])
        self.assertNotIn(target.connection_id, registry.targets)
        self.assertIsNotNone(seen["credentials"])

    def test_remove_refuses_an_unmanaged_live_process(self) -> None:
        target = _target()
        controller, registry, runtime, _ = self._controller(
            target, state="unmanaged_running"
        )
        with self.assertRaisesRegex(ConnectionRuntimeError, "unmanaged live process"):
            controller.remove(target.connection_id)
        with self.assertRaisesRegex(ConnectionRuntimeError, "unsafe restart"):
            controller.restart(target.connection_id)
        self.assertIn(target.connection_id, registry.targets)
        self.assertEqual(runtime.stopped, [])
        self.assertEqual(runtime.forgotten, [])

    def test_stop_requires_a_saved_connection(self) -> None:
        target = _target()
        controller, registry, runtime, _ = self._controller(target, state="running")
        registry.targets.clear()
        with self.assertRaises(ConnectionError):
            controller.stop(target.connection_id)
        self.assertEqual(runtime.stopped, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
