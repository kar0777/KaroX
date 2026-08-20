from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.project_registry import ProjectRegistry, ProjectRegistryError
from karox.web_bridge_profiles import SavedWebBridgeProfile


class ProjectRegistryTests(unittest.TestCase):
    def test_legacy_repository_migrates_to_default_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "KaroX-v5"
            repository.mkdir()

            profile = SavedWebBridgeProfile(
                name="chatgpt-dev",
                target_profile="chatgpt-web",
                tools=("karox.repo.read_file",),
                repository=str(repository),
            )

            self.assertEqual(len(profile.projects), 1)
            self.assertIsNotNone(profile.default_project_id)
            self.assertEqual(profile.projects[0]["project_id"], profile.default_project_id)
            self.assertEqual(Path(profile.projects[0]["path"]), repository.resolve())
            self.assertEqual(Path(profile.repository or ""), repository.resolve())

            restored = SavedWebBridgeProfile.from_dict(profile.to_dict())
            self.assertEqual(restored, profile)

    def test_duplicate_canonical_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "aqurium"
            repository.mkdir()
            first = ProjectRegistry.single(repository)
            with self.assertRaisesRegex(ProjectRegistryError, "duplicate canonical"):
                first.add(repository / ".")

    def test_unsafe_project_ids_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "aqurium"
            repository.mkdir()
            registry = ProjectRegistry.single(repository)
            other = Path(tmp) / "KaroX-v5"
            other.mkdir()
            for project_id in ("../aqurium", "aqurium/child", "aqurium\\child", "a..b"):
                with self.subTest(project_id=project_id):
                    with self.assertRaises(ProjectRegistryError):
                        registry.add(other, project_id=project_id)

    def test_profile_round_trip_preserves_two_approved_projects_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            karox = root / "KaroX-v5"
            aqurium = root / "aqurium"
            karox.mkdir()
            aqurium.mkdir()
            registry = ProjectRegistry.single(karox).add(aqurium)
            aqurium_entry = registry.entry_for_path(aqurium)
            assert aqurium_entry is not None
            registry = registry.with_default(aqurium_entry.project_id)

            profile = SavedWebBridgeProfile(
                name="chatgpt-dev",
                target_profile="chatgpt-web",
                tools=("karox.repo.read_file",),
                repository=str(karox),
                projects=tuple(registry.to_payload()),
                default_project_id=registry.default_project_id,
            )
            restored = SavedWebBridgeProfile.from_dict(profile.to_dict())

            self.assertEqual(len(restored.projects), 2)
            self.assertEqual(restored.default_project_id, aqurium_entry.project_id)
            self.assertEqual(Path(restored.repository or ""), karox.resolve())


if __name__ == "__main__":
    unittest.main()
