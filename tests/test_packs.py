"""Tests for the Phase 8 KaroX Pack SDK.

Unit tests cover the strict manifest schema, path confinement, permission
approval, and the install/enable/disable/remove/doctor lifecycle.  The CLI
tests drive `karox pack` in a subprocess so the full create/install/inspect/
doctor/enable/disable/remove flow is exercised end to end.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.packs import (
    PackAccessDenied,
    PackConfigurationError,
    PackManifestError,
    PackRegistry,
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


class PackCliTests(unittest.TestCase):
    """Full create/install/inspect/doctor/enable/disable/remove CLI flow."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime_dir = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cli(self, *args: str) -> tuple[int, str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(SRC),
                "KAROX_CONFIG_DIR": str(self.root / "config"),
                "KAROX_RUNTIME_DIR": str(self.runtime_dir),
            }
        )
        proc = subprocess.run(
            [sys.executable, "-m", "karox.cli", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_full_lifecycle(self) -> None:
        import json

        target = self.root / "src-pack"
        code, out, _ = self._cli(
            "pack", "create", str(target), "--name", "cli-pack", "--description", "CLI pack", "--json",
        )
        self.assertEqual(code, 0, out)
        self.assertTrue((target / "karox-pack.toml").is_file())

        code, out, _ = self._cli("pack", "install", str(target), "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["name"], "cli-pack")

        code, out, _ = self._cli("pack", "list", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(json.loads(out)), 1)

        code, out, _ = self._cli("pack", "doctor", "cli-pack@0.1.0", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["status"], "ok")

        code, out, _ = self._cli("pack", "enable", "cli-pack@0.1.0", "--json")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["enabled"])

        code, out, _ = self._cli("pack", "disable", "cli-pack@0.1.0", "--json")
        self.assertEqual(code, 0, out)
        self.assertFalse(json.loads(out)["enabled"])

        code, out, _ = self._cli("pack", "remove", "cli-pack@0.1.0", "--json")
        self.assertEqual(code, 0, out)

        code, _, _ = self._cli("pack", "list", "--json")
        self.assertEqual(code, 0)
        self._cli("pack", "list", "--json")


if __name__ == "__main__":
    unittest.main()
