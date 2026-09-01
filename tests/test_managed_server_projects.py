"""Managed dev servers are per project, not per bridge.

The hosted bridge anchors one durable session on one repository, but a saved
profile may approve several projects.  Before these tests the whole
``karox.dev_server.*`` family was resolved against the anchor alone: the
allowlist was discovered there, and every managed process was spawned with the
anchor as its working directory.  On a Python-anchored connection that meant no
server could be started at all, and on a mixed one it meant a Node server would
have been launched inside the wrong tree.

The rules pinned here:

* ``.karox/servers.json`` lets a project declare its own launch recipe, and the
  loopback host is forced no matter what the manifest asks for.
* auto-discovery never promotes a bare ``start`` script when the project also
  ships an explicit ``start:safe`` one, because the bare script is exactly the
  one that turns live publication on in Vacancy Control.
* a recipe approved in project A is refused for a workstream bound to project B.
* the spawned process runs with the *project's* directory as cwd.
* per-project verification commands let a Node project run ``npm test`` on a
  bridge whose saved allowlist only knows pytest.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from _support import ROOT, SRC, initialize_git_repository  # noqa: F401  (path bootstrap)

import karox.hosted_tools_runtime as _htr_mod
from karox.hosted_tools_runtime import (
    DEV_SERVER_START,
    DEV_SERVER_STOP,
    HostedToolsRuntime,
    ManagedServerProfile,
    server_profiles_for_repository,
    server_profiles_from_manifest,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.project_registry import ProjectRegistry
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskStateStore, fact
from mcp.types import CallToolResult


def _write_package(path: Path, scripts: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(
        json.dumps({"name": path.name, "scripts": scripts}), encoding="utf-8"
    )


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> Optional[int]:
        return None


def _fake_identity(pid: int):
    return _htr_mod.ProcessIdentity(
        pid=pid,
        created_at=float(pid),
        executable="node.exe",
        cmdline_digest=f"fake-{pid}",
    )


class ServerManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        from _support import cleanup_temporary_directory

        cleanup_temporary_directory(self.temporary)

    def _manifest(self, payload: Any) -> Path:
        project = self.root / "project"
        (project / ".karox").mkdir(parents=True, exist_ok=True)
        (project / ".karox" / "servers.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return project

    def test_manifest_recipe_is_loaded_and_pinned_to_loopback(self) -> None:
        project = self._manifest(
            {
                "servers": [
                    {
                        "name": "app-live",
                        "argv": ["npm", "start"],
                        "env": {"FEATURE": "on"},
                        "env_allowlist": ["port"],
                        "ready_url": "http://127.0.0.1:3000/api/health",
                    }
                ]
            }
        )
        profiles = server_profiles_from_manifest(project)
        self.assertEqual(len(profiles), 1)
        profile = profiles[0]
        self.assertEqual(profile.argv, ("npm", "start"))
        self.assertEqual(profile.env["FEATURE"], "on")
        # Forced regardless of what the manifest says, so a manifest can never
        # publish the managed server on a routable interface.
        self.assertEqual(profile.env["HOST"], "127.0.0.1")
        self.assertEqual(profile.env_allowlist, frozenset({"PORT"}))
        self.assertEqual(profile.ready_url, "http://127.0.0.1:3000/api/health")

    def test_manifest_cannot_allow_overriding_a_forced_variable(self) -> None:
        project = self._manifest(
            {
                "servers": [
                    {
                        "name": "app",
                        "argv": ["npm", "start"],
                        "env": {"LIVE": "false"},
                        "env_allowlist": ["LIVE", "HOST", "PORT"],
                    }
                ]
            }
        )
        profile = server_profiles_from_manifest(project)[0]
        self.assertEqual(profile.env_allowlist, frozenset({"PORT"}))

    def test_root_level_manifest_is_accepted(self) -> None:
        # An agent cannot create a hidden directory through the repository path
        # guard, so the root-level name must work on its own.
        project = self.root / "root-manifest"
        project.mkdir()
        (project / "karox.servers.json").write_text(
            json.dumps({"servers": [{"name": "app", "argv": ["npm", "start"]}]}),
            encoding="utf-8",
        )
        profiles = server_profiles_from_manifest(project)
        self.assertEqual([item.name for item in profiles], ["app"])

    def test_malformed_manifest_yields_no_profiles(self) -> None:
        project = self._manifest({"servers": [{"name": "broken"}, "nonsense", 5]})
        self.assertEqual(server_profiles_from_manifest(project), ())
        missing = self.root / "no-manifest"
        missing.mkdir()
        self.assertEqual(server_profiles_from_manifest(missing), ())

    def test_non_loopback_host_hint_is_refused(self) -> None:
        project = self._manifest(
            {"servers": [{"name": "public", "argv": ["npm", "start"], "host_hint": "0.0.0.0"}]}
        )
        self.assertEqual(server_profiles_from_manifest(project), ())


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        from _support import cleanup_temporary_directory

        cleanup_temporary_directory(self.temporary)

    def test_bare_start_is_not_approved_when_a_safe_script_exists(self) -> None:
        project = self.root / "vacancy"
        _write_package(
            project,
            {"start": "node scripts/server.mjs", "start:safe": "node scripts/start-safe.mjs"},
        )
        argvs = {item.argv for item in server_profiles_for_repository(project)}
        self.assertIn(("npm", "run", "start:safe"), argvs)
        self.assertNotIn(("npm", "start"), argvs)

    def test_plain_start_is_approved_when_no_safe_variant_exists(self) -> None:
        project = self.root / "plain"
        _write_package(project, {"start": "node server.js"})
        profiles = server_profiles_for_repository(project)
        self.assertEqual([item.argv for item in profiles], [("npm", "start")])
        self.assertEqual(profiles[0].env["HOST"], "127.0.0.1")

    def test_plain_html_project_gets_durable_loopback_static_server(self) -> None:
        project = self.root / "static"
        project.mkdir()
        (project / "aquarium-test.html").write_text("<!doctype html><title>Aquarium</title>", encoding="utf-8")
        profiles = server_profiles_for_repository(project)
        self.assertEqual([item.argv for item in profiles], [("python", "-m", "karox.static_server")])
        self.assertEqual(profiles[0].name, "static-html-loopback")
        self.assertEqual(profiles[0].env, {"HOST": "127.0.0.1", "PORT": "8765"})
        self.assertEqual(profiles[0].env_allowlist, frozenset({"PORT"}))
        self.assertEqual(profiles[0].host_hint, "127.0.0.1")

    def test_html_fallback_does_not_override_a_declared_or_safe_app_server(self) -> None:
        project = self.root / "html-node"
        _write_package(project, {"start": "node server.js"})
        (project / "index.html").write_text("<!doctype html>", encoding="utf-8")
        profiles = server_profiles_for_repository(project)
        self.assertEqual([item.argv for item in profiles], [("npm", "start")])

    def test_html_fallback_is_used_when_package_scripts_are_not_safe_runners(self) -> None:
        project = self.root / "html-unsafe-package"
        _write_package(project, {"start": "docker compose up"})
        (project / "index.html").write_text("<!doctype html>", encoding="utf-8")
        profiles = server_profiles_for_repository(project)
        self.assertEqual([item.argv for item in profiles], [("python", "-m", "karox.static_server")])

    def test_composite_and_unknown_runners_are_refused(self) -> None:
        composite = self.root / "composite"
        _write_package(composite, {"start": "npm run build && node server.js"})
        self.assertEqual(server_profiles_for_repository(composite), ())

        docker = self.root / "docker"
        _write_package(docker, {"start": "docker compose up"})
        self.assertEqual(server_profiles_for_repository(docker), ())

    def test_manifest_wins_over_discovery_for_the_same_argv(self) -> None:
        project = self.root / "both"
        _write_package(project, {"start": "node server.js"})
        (project / ".karox").mkdir()
        (project / ".karox" / "servers.json").write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "name": "declared",
                            "argv": ["npm", "start"],
                            "env_allowlist": ["PORT", "DEBUG"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        profiles = server_profiles_for_repository(project)
        self.assertEqual([item.name for item in profiles], ["declared"])
        self.assertIn("DEBUG", profiles[0].env_allowlist)


class ProjectRoutedServerTests(unittest.TestCase):
    """The managed process runs where the project lives, not where the bridge is anchored."""

    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.anchor = self.root / "anchor"
        initialize_git_repository(self.anchor)
        self.node_project = self.root / "node-app"
        initialize_git_repository(self.node_project)
        _write_package(self.node_project, {"start": "node server.js"})
        for name in ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR"):
            os.environ.pop(name, None)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(self.root / "runtime")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.anchor,
            "managed server project test",
            AccessProfile.ELEVATED,
            session_id="sess-p",
        )
        self.registry = ProjectRegistry.from_profile(
            repository=str(self.anchor),
            projects=(
                {"project_id": "node-app", "path": str(self.node_project), "label": "node"},
            ),
        )
        self.task_states = TaskStateStore(self.sessions)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        from _support import cleanup_temporary_directory

        cleanup_temporary_directory(self.temporary)

    def _runtime(self, popen_factory) -> HostedToolsRuntime:
        return HostedToolsRuntime(
            self.anchor,
            self.sessions,
            "sess-p",
            (DEV_SERVER_START, DEV_SERVER_STOP),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-projects"),
            server_profiles=(),
            popen_factory=popen_factory,
            project_registry=self.registry,
        )

    def _bind_workstream(self, workstream_id: str, project_id: str) -> None:
        self.task_states.bootstrap(
            "sess-p",
            {
                "objective": fact("start the project server", FactOrigin.REPORTED_BY_AGENT),
                "project_id": fact(project_id, FactOrigin.VERIFIED),
            },
            workstream_id=workstream_id,
        )

    def test_server_starts_in_the_workstream_project_directory(self) -> None:
        captured: dict[str, Any] = {}

        def popen_factory(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = kwargs.get("env")
            return _FakeProcess(pid=52001)

        self._bind_workstream("ws-node", "node-app")
        runtime = self._runtime(popen_factory)
        alive = {52001}
        with (
            mock.patch.multiple(
                _htr_mod,
                _pid_alive=lambda pid: pid in alive,
                _kill_pid_tree=alive.discard,
            ),
            mock.patch.object(
                _htr_mod.ProcessIdentity, "capture", side_effect=_fake_identity
            ),
        ):
            result = runtime.execute(
                DEV_SERVER_START,
                {
                    "argv": ["npm", "start"],
                    "process_id": "srv-node",
                    "workstream_id": "ws-node",
                },
                deadline_seconds=10,
            )
        payload = (
            result.structuredContent if isinstance(result, CallToolResult) else result
        )
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(
            os.path.normcase(str(captured["cwd"])),
            os.path.normcase(str(self.node_project.resolve())),
        )
        self.assertEqual(captured["env"]["HOST"], "127.0.0.1")

    def test_recipe_of_another_project_is_refused_for_the_anchor(self) -> None:
        runtime = self._runtime(lambda *a, **k: _FakeProcess(pid=52002))
        result = runtime.execute(
            DEV_SERVER_START,
            {"argv": ["npm", "start"], "process_id": "srv-anchor"},
            deadline_seconds=10,
        )
        payload = (
            result.structuredContent if isinstance(result, CallToolResult) else result
        )
        self.assertFalse(payload.get("ok"))
        self.assertIn("allowlist", payload.get("error", ""))


class PerProjectVerificationTests(unittest.TestCase):
    """A Node project must be able to run its own tests on a Python-anchored bridge."""

    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.anchor = self.root / "anchor"
        initialize_git_repository(self.anchor)
        self.node_project = self.root / "node-app"
        initialize_git_repository(self.node_project)
        _write_package(self.node_project, {"test": "node --test", "lint": "eslint ."})
        for name in ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR"):
            os.environ.pop(name, None)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(self.root / "runtime")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.anchor,
            "verification routing test",
            AccessProfile.ELEVATED,
            session_id="sess-v",
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        from _support import cleanup_temporary_directory

        cleanup_temporary_directory(self.temporary)

    def test_project_commands_extend_the_saved_allowlist(self) -> None:
        from karox.hosted_bridge import CoreToolBridge

        bridge = CoreToolBridge(
            self.anchor,
            self.sessions,
            "sess-v",
            ("karox.repo.read_file", "karox.checks.run"),
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-verify"),
            verification_commands=(("python", "-m", "pytest", "-q"),),
            project_registry=ProjectRegistry.from_profile(
                repository=str(self.anchor),
                projects=(
                    {
                        "project_id": "node-app",
                        "path": str(self.node_project),
                        "label": "node",
                    },
                ),
            ),
        )
        commands = bridge._verification_commands_for(self.node_project)
        self.assertIsNotNone(commands)
        assert commands is not None
        self.assertIn(("python", "-m", "pytest", "-q"), commands)
        self.assertIn(("npm", "test"), commands)
        self.assertIn(("npm", "run", "lint"), commands)


class SavedProfileToolUpgradeTests(unittest.TestCase):
    def test_managed_server_reader_gains_lifecycle_tools(self) -> None:
        from karox.cli import _upgrade_saved_profile_tools
        from karox.web_bridge_profiles import SavedWebBridgeProfile

        profile = SavedWebBridgeProfile(
            name="dev",
            target_profile="chatgpt-web",
            tools=(
                "karox.repo.read_file",
                "karox.dev_server.status",
                "karox.dev_server.logs",
            ),
            access_profile=AccessProfile.WORKSPACE_WRITE,
        )
        upgraded = _upgrade_saved_profile_tools(profile)
        self.assertIn("karox.dev_server.start", upgraded)
        self.assertIn("karox.dev_server.stop", upgraded)
        self.assertIn("karox.dev_server.restart", upgraded)


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    unittest.main()
