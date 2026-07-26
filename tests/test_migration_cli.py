from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.cli import main
from karox.migration import MigrationError, migrate_legacy_metadata


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "legacy"
        self.destination = self.root / "vnext"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_secret_safe_apply_preserves_source_bytes(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        settings = self.source / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "base_url": "https://api.example.invalid/v1",
                    f"api_key_{secret}": secret,
                    "unknown": secret,
                    "model": secret,
                }
            ),
            encoding="utf-8",
        )
        before = settings.read_bytes()
        report = migrate_legacy_metadata(
            self.source, self.destination, dry_run=False
        )
        self.assertTrue(report["applied"])
        self.assertEqual(settings.read_bytes(), before)
        outputs = "\n".join(
            path.read_text(encoding="utf-8") for path in self.destination.iterdir()
        )
        self.assertNotIn(secret, outputs)
        imported = json.loads(
            (self.destination / "imported-settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            imported["settings"],
            {"base_url": "https://api.example.invalid/v1", "theme": "dark"},
        )

    def test_empty_source_is_diagnostic_in_dry_run_and_refused_on_apply(self) -> None:
        report = migrate_legacy_metadata(self.source, self.destination)
        self.assertEqual(report["diagnostic"], "no_supported_source_files")
        self.assertFalse(report["applied"])
        with self.assertRaisesRegex(MigrationError, "no supported"):
            migrate_legacy_metadata(self.source, self.destination, dry_run=False)

    def test_apply_refuses_to_overwrite_previous_output(self) -> None:
        (self.source / "settings.json").write_text(
            json.dumps({"theme": "dark"}), encoding="utf-8"
        )
        self.destination.mkdir()
        (self.destination / "migration-report.json").write_text(
            "do not replace", encoding="utf-8"
        )
        with self.assertRaisesRegex(MigrationError, "refusing to overwrite"):
            migrate_legacy_metadata(self.source, self.destination, dry_run=False)
        self.assertEqual(
            (self.destination / "migration-report.json").read_text(encoding="utf-8"),
            "do not replace",
        )

    def test_symlink_source_is_reported_and_never_read(self) -> None:
        outside = self.root / "outside.json"
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        outside.write_text(json.dumps({"theme": secret}), encoding="utf-8")
        try:
            os.symlink(outside, self.source / "settings.json")
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        report = migrate_legacy_metadata(self.source, self.destination)
        self.assertEqual(report["diagnostic"], "no_supported_source_files")
        self.assertEqual(
            report["invalid_fields"],
            [{"file": "settings.json", "reason": "link_or_reparse_point"}],
        )
        self.assertNotIn(secret, json.dumps(report))


class CliSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "KAROX_VNEXT_CONFIG_DIR": str(self.root / "config"),
                "KAROX_VNEXT_RUNTIME_DIR": str(self.root / "runtime"),
                "KAROX_LEGACY_CONFIG_DIR": str(self.root / "legacy"),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def invoke(self, arguments: list[str]) -> tuple[int, object]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def test_no_arguments_opens_interactive_cli_in_current_repository(self) -> None:
        with patch("karox.tui.run_tui", return_value=17) as run_tui:
            code = main([])

        self.assertEqual(code, 17)
        run_tui.assert_called_once_with(repository=str(Path.cwd().resolve()))

    def test_paths_and_session_commands(self) -> None:
        code, paths = self.invoke(["paths", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(paths["runtime_dir"], str((self.root / "runtime").resolve()))

        code, created = self.invoke(
            [
                "session",
                "create",
                "--repository",
                str(self.repository),
                "--task",
                "smoke task",
                "--id",
                "cli-smoke",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(created["session_id"], "cli-smoke")
        code, records = self.invoke(["session", "list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual([item["session_id"] for item in records], ["cli-smoke"])
        code, shown = self.invoke(["session", "show", "cli-smoke", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(shown["task"], "smoke task")

        code, revoked = self.invoke(["session", "revoke", "cli-smoke", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(revoked["revoked"])
        self.assertEqual(revoked["status"], "revoked")

    def test_top_level_doctor_reports_every_runtime_scope(self) -> None:
        code, report = self.invoke(["doctor", "--json"])
        self.assertEqual(code, 0)
        self.assertIn(report["status"], {"ok", "degraded"})
        self.assertEqual(
            set(report["checks"]),
            {
                "provider_credentials",
                "mcp_credentials",
                "bridge_credentials",
                "sessions",
                "packs",
            },
        )

    def test_migration_dry_run_command(self) -> None:
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "settings.json").write_text(
            json.dumps({"locale": "ru"}), encoding="utf-8"
        )
        code, report = self.invoke(["migrate", "--json"])
        self.assertEqual(code, 0)
        self.assertFalse(report["applied"])
        self.assertEqual(report["imported_fields"], ["locale"])


if __name__ == "__main__":
    unittest.main()
