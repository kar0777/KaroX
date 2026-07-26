"""Tests for the Phase 8 KaroX Pack SDK.

Unit tests cover the strict manifest schema, path confinement, permission
approval, and the install/enable/disable/remove/doctor lifecycle.  The CLI
tests drive `karox pack` in a subprocess so the full create/install/inspect/
doctor/enable/disable/remove flow is exercised end to end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC, child_environment  # noqa: F401 - inserts src on sys.path

import karox.packs

from karox.packs import (
    PackAccessDenied,
    PackConfigurationError,
    PackManifestError,
    PackRegistry,
    _exclusive_file_lock,
    create_pack_template,
    parse_pack_manifest,
)


def _toml_array(items: list) -> str:
    if not items:
        return "[]"
    inner = ", ".join(f'"{item}"' for item in items)
    return f"[{inner}]"


def _manifest_text(**overrides: object) -> str:
    base = {
        "name": "sample-pack",
        "version": "0.1.0",
        "description": "A sample pack.",
        "authors": ["KaroX"],
        "license": "MIT",
        "karox_version": "5.x",
        "platforms": ["windows", "linux", "macos"],
        "skills": ["skills/SKILL.md"],
        "mcp": [],
        "detectors": ["detectors/generic.txt"],
        "commands": [],
        "templates": [],
        "tests": ["tests/pack_test.txt"],
        "health_checks": [],
        "permissions": [],
        "tool_name": "meta",
        "tool_capability": "repo.read",
        "tool_description": "d",
        "tool_mutates": False,
    }
    base.update(overrides)
    parts = [
        "manifest_version = 1",
        f'name = "{base["name"]}"',
        f'version = "{base["version"]}"',
        f'description = "{base["description"]}"',
        f'authors = {_toml_array(base["authors"])}',
        f'license = "{base["license"]}"',
        f'karox_version = "{base["karox_version"]}"',
        f'platforms = {_toml_array(base["platforms"])}',
        # tools as an inline table array so subsequent keys stay top-level.
        f'tools = [{{ name = "{base["tool_name"]}", capability = "{base["tool_capability"]}", '
        f'description = "{base["tool_description"]}", mutates = {"true" if base["tool_mutates"] else "false"} }}]',
        f'skills = {_toml_array(base["skills"])}',
        f'detectors = {_toml_array(base["detectors"])}',
        f'commands = {_toml_array(base["commands"])}',
        f'templates = {_toml_array(base["templates"])}',
        f'tests = {_toml_array(base["tests"])}',
        f'health_checks = {_toml_array(base["health_checks"])}',
        f'permissions = {_toml_array(base["permissions"])}',
    ]
    if base["mcp"]:
        for decl in base["mcp"]:
            parts.append("[[mcp]]")
            for key, val in decl.items():
                parts.append(f'  {key} = "{val}"')
    return "\n".join(parts) + "\n"


def _write_pack(root: Path, **overrides: object) -> Path:
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "SKILL.md").write_text("---\nname: s\ndescription: d\nversion: 0.1.0\n---\nbody\n", encoding="utf-8")
    (root / "detectors").mkdir(parents=True, exist_ok=True)
    (root / "detectors" / "generic.txt").write_text("marker\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "pack_test.txt").write_text("test\n", encoding="utf-8")
    (root / "karox-pack.toml").write_text(_manifest_text(**overrides), encoding="utf-8")
    return root


class PackManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "pack"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_manifest_parses(self) -> None:
        _write_pack(self.root)
        manifest = parse_pack_manifest(self.root / "karox-pack.toml")
        self.assertEqual(manifest.name, "sample-pack")
        self.assertEqual(manifest.version, "0.1.0")
        self.assertEqual(manifest.tools[0].capability, "repo.read")
        self.assertEqual(manifest.requested_capabilities(), frozenset({"repo.read"}))

    def test_unknown_manifest_field_rejected(self) -> None:
        _write_pack(self.root)
        path = self.root / "karox-pack.toml"
        path.write_text(path.read_text(encoding="utf-8") + 'publisher = "x"\n', encoding="utf-8")
        with self.assertRaisesRegex(PackManifestError, "unknown pack manifest fields"):
            parse_pack_manifest(path)

    def test_unsupported_manifest_version_rejected(self) -> None:
        _write_pack(self.root)
        path = self.root / "karox-pack.toml"
        text = path.read_text(encoding="utf-8").replace("manifest_version = 1", "manifest_version = 99")
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(PackManifestError, "unsupported pack manifest version"):
            parse_pack_manifest(path)

    def test_bad_name_and_version_rejected(self) -> None:
        _write_pack(self.root, name="Bad Name")
        with self.assertRaisesRegex(PackManifestError, "name must be lowercase kebab"):
            parse_pack_manifest(self.root / "karox-pack.toml")
        _write_pack(self.root, version="1.0")
        with self.assertRaisesRegex(PackManifestError, "semantic x.y.z"):
            parse_pack_manifest(self.root / "karox-pack.toml")

    def test_network_requires_process_run(self) -> None:
        _write_pack(self.root, permissions=["network"])
        with self.assertRaisesRegex(PackManifestError, "network permission requires process.run"):
            parse_pack_manifest(self.root / "karox-pack.toml")

    def test_unsupported_permission_rejected(self) -> None:
        _write_pack(self.root, permissions=["evil"])
        with self.assertRaisesRegex(PackManifestError, "unsupported pack permission"):
            parse_pack_manifest(self.root / "karox-pack.toml")

    def test_unsupported_platform_rejected(self) -> None:
        _write_pack(self.root, platforms=["bsd"])
        with self.assertRaisesRegex(PackManifestError, "unsupported platform"):
            parse_pack_manifest(self.root / "karox-pack.toml")

    def test_malformed_tool_entries_are_rejected_not_coerced(self) -> None:
        _write_pack(self.root)
        path = self.root / "karox-pack.toml"
        text = path.read_text(encoding="utf-8")
        text = text.replace("mutates = false", 'mutates = "false"')
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(PackManifestError, "mutates must be boolean"):
            parse_pack_manifest(path)


class PackLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "inspector"
        create_pack_template(self.source, name="project-inspector", description="Sample pack")
        self.registry = PackRegistry(self.root / "registry")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_install_doctor_enable_disable_remove_roundtrip(self) -> None:
        pack = self.registry.install(self.source)
        self.assertFalse(pack.enabled)
        self.assertEqual(self.registry.doctor(pack.identity)["status"], "ok")
        self.assertTrue(self.registry.enable(pack.identity).enabled)
        self.assertFalse(self.registry.disable(pack.identity).enabled)
        self.registry.remove(pack.identity)
        self.assertEqual(self.registry.list(), [])

    def test_duplicate_install_rejected(self) -> None:
        self.registry.install(self.source)
        with self.assertRaisesRegex(PackConfigurationError, "already installed"):
            self.registry.install(self.source)

    def test_remove_enabled_pack_rejected(self) -> None:
        pack = self.registry.install(self.source)
        self.registry.enable(pack.identity)
        with self.assertRaisesRegex(PackConfigurationError, "cannot remove an enabled pack"):
            self.registry.remove(pack.identity)

    def test_enable_refuses_a_pack_whose_content_changed_since_install(self) -> None:
        """Enabling is when a Pack's code becomes callable, so it re-verifies.

        The install directory is ordinary files. Verifying only at install time
        meant a Pack whose skill had been rewritten afterwards was activated in
        silence, with the registry still vouching for the original hashes.
        """
        pack = self.registry.install(self.source)
        tampered = Path(pack.install_path) / "skills" / "SKILL.md"
        tampered.write_text("---\nname: s\ndescription: d\nversion: 0.1.0\n---\nowned\n", encoding="utf-8")

        with self.assertRaisesRegex(PackConfigurationError, "refusing to enable.*were modified"):
            self.registry.enable(pack.identity)
        self.assertFalse(self.registry.get(pack.identity).enabled)

    def test_enable_refuses_a_pack_whose_manifest_changed_since_install(self) -> None:
        pack = self.registry.install(self.source)
        manifest = Path(pack.install_path) / "karox-pack.toml"
        text = manifest.read_text(encoding="utf-8").replace(
            'description = "Sample pack"', 'description = "Rewritten after install"'
        )
        self.assertIn("Rewritten", text)
        manifest.write_text(text, encoding="utf-8")

        with self.assertRaisesRegex(PackConfigurationError, "refusing to enable.*manifest changed"):
            self.registry.enable(pack.identity)
        self.assertFalse(self.registry.get(pack.identity).enabled)

    def test_enable_refuses_a_pack_whose_declared_file_was_deleted(self) -> None:
        pack = self.registry.install(self.source)
        (Path(pack.install_path) / "detectors" / "generic.txt").unlink()

        with self.assertRaisesRegex(PackConfigurationError, "refusing to enable.*are missing"):
            self.registry.enable(pack.identity)
        self.assertFalse(self.registry.get(pack.identity).enabled)

    def test_unapproved_capability_rejected(self) -> None:
        # The sample pack requests only repo.read, which requires no extra
        # permission approval; force a mismatch by approving nothing.
        self.registry.install(self.source)
        # A pack that requests process.run+network needs explicit approval.
        source2 = self.root / "runner"
        create_pack_template(source2, name="runner-pack", description="Runner pack")
        # Rewrite its manifest to request permissions.
        path = source2 / "karox-pack.toml"
        path.write_text(path.read_text(encoding="utf-8").replace("permissions = []", 'permissions = ["process.run", "network"]'), encoding="utf-8")
        with self.assertRaisesRegex(PackAccessDenied, "unapproved permissions"):
            self.registry.install(source2)
        # Approving all requested permissions succeeds.
        self.registry.install(source2, approved_permissions=["process.run", "network"])

    def test_path_traversal_reference_rejected(self) -> None:
        # Add a traversing reference to the manifest.
        path = self.source / "karox-pack.toml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'detectors = ["detectors/generic.txt"]',
                'detectors = ["../../../etc/passwd"]',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PackManifestError, "escapes the pack directory"):
            self.registry.install(self.source)

    def test_doctor_detects_broken_pack(self) -> None:
        pack = self.registry.install(self.source)
        # Tamper: delete a referenced file from the installed copy.
        install_path = Path(pack.install_path)
        (install_path / "detectors" / "generic.txt").unlink()
        result = self.registry.doctor(pack.identity)
        self.assertEqual(result["status"], "broken")
        self.assertTrue(result["missing_files"])

    def test_doctor_detects_modified_pack_content(self) -> None:
        pack = self.registry.install(self.source)
        install_path = Path(pack.install_path)
        (install_path / "detectors" / "generic.txt").write_text(
            "tampered\n", encoding="utf-8"
        )
        result = self.registry.doctor(pack.identity)
        self.assertEqual(result["status"], "broken")
        self.assertEqual(result["modified_files"], ["detectors/generic.txt"])

    def test_install_does_not_copy_undeclared_content(self) -> None:
        (self.source / "undeclared.txt").write_text("not installed\n", encoding="utf-8")
        pack = self.registry.install(self.source)
        self.assertFalse((Path(pack.install_path) / "undeclared.txt").exists())

    def test_install_rejects_incompatible_version_and_platform(self) -> None:
        manifest = self.source / "karox-pack.toml"
        original = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            original.replace('karox_version = "5.x"', 'karox_version = "99.x"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PackConfigurationError, "incompatible"):
            self.registry.install(self.source)

        current = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
        other = next(item for item in ("windows", "linux", "macos") if item != current)
        manifest.write_text(
            original.replace(
                'platforms = ["windows", "linux", "macos"]',
                f'platforms = ["{other}"]',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PackConfigurationError, "current platform"):
            self.registry.install(self.source)

    def test_template_description_is_toml_escaped(self) -> None:
        target = self.root / "quoted"
        create_pack_template(
            target, name="quoted-pack", description='He said "go"\\now',
        )
        manifest = parse_pack_manifest(target / "karox-pack.toml")
        self.assertEqual(manifest.description, 'He said "go"\\now')


class PackTransientLockTests(unittest.TestCase):
    """A passing file lock must not become a user-visible failure.

    Windows refuses a delete or a rename while any other process holds a handle,
    and a virus scanner or the search indexer opening a freshly installed pack is
    enough. `karox pack remove` was answering that with "cannot remove installed
    pack" and exit 2 -- for a condition that had already cleared.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.source = self.root / "inspector"
        create_pack_template(self.source, name="project-inspector", description="Sample pack")
        self.registry = PackRegistry(self.root / "registry")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_remove_survives_a_lock_that_clears(self) -> None:
        """Deterministic on every platform: fail the first attempt, then relent."""
        pack = self.registry.install(self.source)
        attempts: list[int] = []
        real_rmtree = shutil.rmtree

        def flaky_rmtree(target, *args, **kwargs):  # type: ignore[no-untyped-def]
            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError(13, "The process cannot access the file")
            return real_rmtree(target, *args, **kwargs)

        with patch.object(karox.packs.shutil, "rmtree", flaky_rmtree):
            self.registry.remove(pack.identity)

        self.assertGreaterEqual(len(attempts), 2, "the removal was not retried")
        self.assertEqual(self.registry.list(), [])
        self.assertFalse(Path(pack.install_path).exists())

    def test_remove_still_reports_a_lock_that_never_clears(self) -> None:
        """The retry must not turn a real, persistent failure into silence."""
        pack = self.registry.install(self.source)

        def always_locked(target, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise PermissionError(13, "The process cannot access the file")

        with patch.object(karox.packs.shutil, "rmtree", always_locked):
            with self.assertRaisesRegex(PackConfigurationError, "cannot remove installed pack"):
                self.registry.remove(pack.identity)
        # Still installed: a failed removal must not drop the registry entry.
        self.assertEqual([p.identity for p in self.registry.list()], [pack.identity])

    @unittest.skipUnless(os.name == "nt", "only Windows refuses to delete an open file")
    def test_remove_survives_a_real_held_handle_on_windows(self) -> None:
        """The same thing with an actual OS handle rather than a patched call.

        This is the observed failure: a full suite run held a handle on a pack
        file long enough for `pack remove` to exit 2 with nothing on stdout.
        """
        pack = self.registry.install(self.source)
        held = open(Path(pack.install_path) / "skills" / "SKILL.md", "rb")
        releaser = threading.Timer(0.4, held.close)
        releaser.start()
        try:
            self.registry.remove(pack.identity)
        finally:
            releaser.cancel()
            held.close()
        self.assertEqual(self.registry.list(), [])


class PackRegistryConcurrencyTests(unittest.TestCase):
    """The registry's read-modify-write has to survive a second writer."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lock_is_held_against_another_process(self) -> None:
        """The lock has to be an OS lock, not a process-local one.

        Two `karox pack` commands are two processes, so a `threading.Lock` would
        not see the other at all. This holds the lock from a real child and
        asserts the parent cannot take it.
        """
        lock_path = self.root / "packs.json.lock"
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                "import sys; sys.path.insert(0, sys.argv[1]);"
                "from pathlib import Path;"
                "from karox.packs import _exclusive_file_lock;"
                "\nwith _exclusive_file_lock(Path(sys.argv[2])):\n"
                "    print('held', flush=True)\n"
                "    sys.stdin.readline()\n",
                str(SRC), str(lock_path),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout is not None and holder.stdin is not None
            # Read stderr only once the child is gone: it is a pipe, and reading
            # it while the child still waits on stdin would deadlock the test.
            if holder.stdout.readline().strip() != "held":
                holder.kill()
                holder.wait(timeout=30)
                reason = holder.stderr.read() if holder.stderr else ""
                self.fail(f"the lock holder never started: {reason.strip() or 'no output'}")

            with self.assertRaisesRegex(PackConfigurationError, "timed out.*waiting for the pack registry lock"):
                with _exclusive_file_lock(lock_path, timeout=0.3):
                    pass

            # Releasing it must hand the lock over, not leave the file poisoned.
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=30)
            with _exclusive_file_lock(lock_path, timeout=5):
                pass
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=30)

    def test_racing_installs_all_survive(self) -> None:
        """No install may be lost to a concurrent one.

        Each writer loaded the registry, worked, then saved the snapshot it had
        read. Whoever saved last therefore erased every pack installed in the
        meantime. The registries here are separate instances -- as separate as
        two processes -- so nothing but the file lock serialises them.
        """
        names = [f"racer-{index}" for index in range(6)]
        for name in names:
            create_pack_template(self.root / name, name=name, description="Racer")

        registry_root = self.root / "registry"
        PackRegistry(registry_root)  # create the root once, off the hot path
        start = threading.Barrier(len(names))
        failures: list[BaseException] = []

        def install(name: str) -> None:
            try:
                start.wait(timeout=30)
                PackRegistry(registry_root).install(self.root / name)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(exc)

        workers = [threading.Thread(target=install, args=(name,)) for name in names]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
            self.assertFalse(worker.is_alive(), "an installing thread did not finish")

        self.assertEqual(failures, [])
        installed = sorted(pack.name for pack in PackRegistry(registry_root).list())
        self.assertEqual(installed, sorted(names))


class PackCliTests(unittest.TestCase):
    """Full create/install/inspect/doctor/enable/disable/remove CLI flow."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime_dir = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cli(self, *args: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "karox.cli", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=child_environment(
                config_dir=self.root / "config",
                runtime_dir=self.runtime_dir,
                PYTHONPATH=str(SRC),
            ),
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _ok(self, *args: str) -> str:
        """Run a pack command that must succeed, and say why if it did not.

        The CLI reports a refusal on stderr, so asserting with stdout produced a
        bare "2 != 0" that named neither the command nor the reason.
        """
        code, out, err = self._cli(*args)
        self.assertEqual(
            code, 0, f"karox {' '.join(args)} exited {code}: {err.strip() or out.strip()}"
        )
        return out

    def test_full_lifecycle(self) -> None:
        import json

        target = self.root / "src-pack"
        self._ok(
            "pack", "create", str(target), "--name", "cli-pack", "--description", "CLI pack", "--json",
        )
        self.assertTrue((target / "karox-pack.toml").is_file())

        out = self._ok("pack", "install", str(target), "--json")
        self.assertEqual(json.loads(out)["name"], "cli-pack")

        out = self._ok("pack", "list", "--json")
        self.assertEqual(len(json.loads(out)), 1)

        out = self._ok("pack", "doctor", "cli-pack@0.1.0", "--json")
        self.assertEqual(json.loads(out)["status"], "ok")

        out = self._ok("pack", "enable", "cli-pack@0.1.0", "--json")
        self.assertTrue(json.loads(out)["enabled"])

        out = self._ok("pack", "disable", "cli-pack@0.1.0", "--json")
        self.assertFalse(json.loads(out)["enabled"])

        self._ok("pack", "remove", "cli-pack@0.1.0", "--json")

        # The removal has to be observable, not merely un-refused: the old
        # assertion only checked that listing exited zero, which it does
        # whether or not the pack is actually gone.
        out = self._ok("pack", "list", "--json")
        self.assertEqual(json.loads(out), [])


if __name__ == "__main__":
    unittest.main()
