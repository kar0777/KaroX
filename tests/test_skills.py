from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.models import AccessProfile, Capability
from karox.policy import CapabilityPolicy
from karox.skills import (
    SkillCatalog,
    SkillError,
    SkillPermission,
    configure_skill_policy,
    skill_selection,
    skill_system_prompt,
    validate_selection,
)


class SkillCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def write_skill(
        source: Path,
        name: str,
        *,
        version: str = "1.2.3",
        metadata: str = "",
        body: str = "Follow the bounded instructions.\n",
        references: dict[str, str | bytes] | None = None,
    ) -> Path:
        directory = source / name
        directory.mkdir(parents=True, exist_ok=True)
        extra = metadata.rstrip()
        if extra:
            extra += "\n"
        manifest = (
            "---\n"
            f"name: {name}\n"
            f"description: {name} test Skill\n"
            f"version: {version}\n"
            f"{extra}"
            "---\n"
            f"{body}"
        )
        (directory / "SKILL.md").write_bytes(manifest.encode("utf-8"))
        for relative, value in (references or {}).items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, bytes):
                target.write_bytes(value)
            else:
                target.write_text(value, encoding="utf-8")
        return directory

    def make_repository(self, name: str) -> tuple[Path, Path]:
        repository = self.root / name
        repository.mkdir()
        return repository, repository / ".karox" / "skills"

    def test_source_precedence_and_shadow_diagnostics_are_deterministic(self) -> None:
        karox = self.repository / ".karox" / "skills"
        agents = self.repository / ".agents" / "skills"
        claude = self.repository / ".claude" / "skills"
        extra = self.root / "extra"
        global_directory = self.root / "global"
        for source, marker in (
            (global_directory, "global"),
            (extra, "extra"),
            (claude, "claude"),
            (agents, "agents"),
            (karox, "karox"),
        ):
            self.write_skill(source, "shared", body=f"{marker}\n")

        catalog = SkillCatalog(
            self.repository,
            extra_directories=(extra,),
            global_directory=global_directory,
        )

        metadata = catalog.get("shared")
        self.assertEqual(metadata.source_kind, "repository_karox")
        self.assertEqual(catalog.load("shared").instructions, "karox\n")
        shadowed = [item for item in catalog.diagnostics if item.status == "shadowed"]
        self.assertEqual(len(shadowed), 4)
        self.assertTrue(
            all(item.selected_path == str(metadata.directory) for item in shadowed)
        )

    def test_discovery_is_metadata_only_and_content_failures_are_lazy(self) -> None:
        source = self.repository / ".karox" / "skills"
        invalid_body = self.write_skill(source, "invalid-body")
        prefix = (invalid_body / "SKILL.md").read_bytes().split(b"---\n", 2)
        (invalid_body / "SKILL.md").write_bytes(
            b"---\n" + prefix[1] + b"---\n\xff"
        )
        self.write_skill(
            source,
            "missing-reference",
            metadata="files:\n  - missing.txt",
        )
        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")

        self.assertEqual(
            {item.name for item in catalog.discover()},
            {"invalid-body", "missing-reference"},
        )
        with self.assertRaisesRegex(SkillError, "valid UTF-8"):
            catalog.load("invalid-body")
        with self.assertRaisesRegex(SkillError, "does not exist"):
            catalog.load("missing-reference")

    def test_metadata_aliases_work_and_conflicts_fail_closed(self) -> None:
        source = self.repository / ".karox" / "skills"
        self.write_skill(
            source,
            "aliases",
            metadata=(
                "metadata:\n"
                "  project_types: [python]\n"
                "  mcp: [docs/search]\n"
                "  validation: [run unit tests]\n"
                "  referenced_files: [notes.txt]"
            ),
            references={"notes.txt": "reference\n"},
        )
        self.write_skill(
            source,
            "conflict",
            metadata=(
                "compatible_project_types: [python]\n"
                "project_types: [rust]"
            ),
        )
        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")

        aliases = catalog.get("aliases")
        self.assertEqual(aliases.compatible_project_types, ("python",))
        self.assertEqual(aliases.optional_mcp, ("docs/search",))
        self.assertEqual(aliases.validation_rules, ("run unit tests",))
        self.assertEqual(aliases.files, ("notes.txt",))
        rejected = [item for item in catalog.diagnostics if item.name == "conflict"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("conflicting", rejected[0].reason)

    def test_falsey_non_list_permissions_and_files_fail_closed(self) -> None:
        source = self.repository / ".karox" / "skills"
        self.write_skill(
            source,
            "invalid-permissions",
            metadata="permissions: {}",
        )
        self.write_skill(
            source,
            "invalid-files",
            metadata="files: ''",
        )
        self.write_skill(
            source,
            "valid-empty-lists",
            metadata="permissions: []\nfiles: []",
        )

        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")

        self.assertEqual(
            [item.name for item in catalog.discover()], ["valid-empty-lists"]
        )
        rejected = {item.name: item.reason for item in catalog.diagnostics}
        self.assertIn("permissions must be a list", rejected["invalid-permissions"])
        self.assertIn("files must be a list", rejected["invalid-files"])

    def test_unknown_required_tools_are_rejected_during_discovery(self) -> None:
        source = self.repository / ".karox" / "skills"
        self.write_skill(
            source,
            "unknown-tool",
            metadata="required_tools: [repo_read_file, shell_everything]",
        )
        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")

        self.assertEqual(catalog.discover(), ())
        self.assertIn("unknown required Agent tool", catalog.diagnostics[0].reason)

    def test_unsafe_and_linked_references_are_rejected(self) -> None:
        source = self.repository / ".karox" / "skills"
        self.write_skill(source, "traversal", metadata="files: [../secret.txt]")
        self.write_skill(source, "absolute", metadata="files: ['C:/secret.txt']")
        linked = self.write_skill(source, "linked", metadata="files: [linked.txt]")
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        try:
            os.symlink(outside, linked / "linked.txt")
        except OSError:
            link_supported = False
        else:
            link_supported = True

        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")
        names = {item.name for item in catalog.discover()}
        self.assertNotIn("traversal", names)
        self.assertNotIn("absolute", names)
        if link_supported:
            self.assertIn("linked", names)
            with self.assertRaisesRegex(SkillError, "links or reparse points"):
                catalog.load("linked")

    def test_size_and_reference_count_limits_fail_closed(self) -> None:
        repository, source = self.make_repository("metadata-limit")
        self.write_skill(source, "large-metadata", metadata="tags: ['" + "x" * 100 + "']")
        with patch.object(SkillCatalog, "MAX_METADATA_BYTES", 96):
            catalog = SkillCatalog(repository, global_directory=self.root / "none")
            self.assertEqual(catalog.discover(), ())
            self.assertIn("metadata exceeds", catalog.diagnostics[0].reason)

        repository, source = self.make_repository("manifest-limit")
        self.write_skill(source, "large-manifest", body="x" * 300)
        with patch.object(SkillCatalog, "MAX_MANIFEST_BYTES", 160):
            catalog = SkillCatalog(repository, global_directory=self.root / "none")
            self.assertEqual(catalog.discover(), ())
            self.assertIn("manifest exceeds", catalog.diagnostics[0].reason)

        repository, source = self.make_repository("reference-limit")
        self.write_skill(
            source,
            "large-reference",
            metadata="files: [large.txt]",
            references={"large.txt": "0123456789"},
        )
        with patch.object(SkillCatalog, "MAX_REFERENCE_BYTES", 8):
            catalog = SkillCatalog(repository, global_directory=self.root / "none")
            with self.assertRaisesRegex(SkillError, "8-byte limit"):
                catalog.load("large-reference")

        repository, source = self.make_repository("aggregate-limit")
        directory = self.write_skill(
            source,
            "large-total",
            metadata="files: [one.txt]",
            references={"one.txt": "0123456789"},
        )
        manifest_size = (directory / "SKILL.md").stat().st_size
        with patch.object(SkillCatalog, "MAX_TOTAL_BYTES", manifest_size + 5):
            catalog = SkillCatalog(repository, global_directory=self.root / "none")
            with self.assertRaisesRegex(SkillError, "total limit"):
                catalog.load("large-total")

        repository, source = self.make_repository("reference-count")
        self.write_skill(source, "many-files", metadata="files: [one.txt, two.txt]")
        with patch.object(SkillCatalog, "MAX_REFERENCES", 1):
            catalog = SkillCatalog(repository, global_directory=self.root / "none")
            self.assertEqual(catalog.discover(), ())
            self.assertIn("more than 1", catalog.diagnostics[0].reason)

    def test_manifest_changes_after_discovery_require_rediscovery(self) -> None:
        source = self.repository / ".karox" / "skills"
        directory = self.write_skill(source, "mutable")
        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")
        catalog.discover()
        with (directory / "SKILL.md").open("a", encoding="utf-8") as handle:
            handle.write("changed\n")

        with self.assertRaisesRegex(SkillError, "changed after discovery"):
            catalog.load("mutable")

    def test_duplicate_keys_aliases_and_invalid_utf8_are_rejected(self) -> None:
        source = self.repository / ".karox" / "skills"
        cases = {
            "duplicate": (
                b"---\nname: duplicate\ndescription: first\n"
                b"description: second\n---\nbody\n"
            ),
            "yaml-alias": (
                b"---\nname: yaml-alias\ndescription: &value text\n"
                b"author: *value\n---\nbody\n"
            ),
            "invalid-utf8": (
                b"---\nname: invalid-utf8\ndescription: bad-\xff\n---\nbody\n"
            ),
        }
        for name, value in cases.items():
            directory = source / name
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_bytes(value)

        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")
        self.assertEqual(catalog.discover(), ())
        reasons = {item.name: item.reason for item in catalog.diagnostics}
        self.assertIn("duplicate", reasons["duplicate"])
        self.assertIn("aliases are not allowed", reasons["yaml-alias"])
        self.assertIn("valid UTF-8", reasons["invalid-utf8"])

    def test_semantic_versions_are_strict(self) -> None:
        source = self.repository / ".karox" / "skills"
        self.write_skill(
            source,
            "valid-version",
            version="1.2.3-alpha.1+build.5",
        )
        for name, version in (
            ("leading-zero", "01.2.3"),
            ("short-version", "1.2"),
            ("zero-prerelease", "1.2.3-01"),
        ):
            self.write_skill(source, name, version=version)
        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")

        self.assertEqual([item.name for item in catalog.discover()], ["valid-version"])
        self.assertEqual(catalog.get("valid-version").version, "1.2.3-alpha.1+build.5")

    def test_permission_decisions_bind_identity_and_intersect_access_profile(self) -> None:
        source = self.repository / ".karox" / "skills"
        self.write_skill(
            source,
            "permissions",
            metadata=(
                "permissions:\n"
                "  - repo.read\n"
                "  - repo.write\n"
                "  - process.run\n"
                "  - network"
            ),
        )
        metadata = SkillCatalog(
            self.repository, global_directory=self.root / "none"
        ).get("permissions")
        selection = skill_selection(
            metadata,
            {
                Capability.REPO_READ: SkillPermission.ALLOW,
                Capability.REPO_WRITE: SkillPermission.DENY,
                Capability.NETWORK: SkillPermission.ALLOW,
            },
        )
        self.assertEqual(selection["permissions"]["process.run"], "ask")

        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        origin = configure_skill_policy(policy, metadata, selection, parent="native_agent:x")
        self.assertTrue(policy.decide(origin, Capability.REPO_READ).allowed)
        self.assertEqual(
            policy.decide(origin, Capability.REPO_WRITE).reason, "origin deny"
        )
        self.assertFalse(policy.decide(origin, Capability.PROCESS_RUN).allowed)
        self.assertFalse(policy.decide(origin, Capability.NETWORK).allowed)

        changed = dict(selection)
        changed["metadata_sha256"] = "0" * 64
        with self.assertRaisesRegex(SkillError, "metadata_sha256"):
            validate_selection(metadata, changed)

    def test_validation_metadata_is_inert_prompt_data(self) -> None:
        source = self.repository / ".karox" / "skills"
        self.write_skill(
            source,
            "inert-validation",
            metadata="validation_rules: [SECRET_VALIDATOR_DIRECTIVE]",
            body="Visible instructions.\n",
            references={"unused.txt": "unused"},
        )
        catalog = SkillCatalog(self.repository, global_directory=self.root / "none")
        content = catalog.load("inert-validation")
        prompt = skill_system_prompt(content)

        self.assertIn("Visible instructions", prompt)
        self.assertNotIn("SECRET_VALIDATOR_DIRECTIVE", prompt)


if __name__ == "__main__":
    unittest.main()
