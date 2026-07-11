#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import zipfile
from pathlib import Path


def load_admin(root: Path, temp: Path):
    os.environ["HOME"] = str(temp / "home")
    os.environ["APPDATA"] = str(temp / "appdata")
    os.environ["LOCALAPPDATA"] = str(temp / "localappdata")
    os.environ["XDG_CONFIG_HOME"] = str(temp / "xdg-config")
    os.environ["XDG_DATA_HOME"] = str(temp / "xdg-data")
    spec = importlib.util.spec_from_file_location("karox_admin_tested", root / "scripts" / "karox_admin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="karox-admin-test-") as raw_temp:
        temp = Path(raw_temp)
        admin = load_admin(root, temp)

        assert admin.semver("v3.12.0") > admin.semver("3.11.9")
        assert admin.redact({"apiKey": "secret"})["apiKey"] == "[REDACTED]"
        assert "[REDACTED]" in admin.redact_string("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")

        admin.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (admin.CONFIG_DIR / "settings.json").write_text(
            json.dumps({"language": "ru", "aiClient": "notion", "tunnelProvider": "cloudflare"}),
            encoding="utf-8",
        )
        session_dir = admin.SESSIONS_DIR / "session-test"
        logs_dir = session_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        secret = "this-is-a-super-secret-session-key-1234567890"
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "id": "session-test",
                    "title": "Test session",
                    "repo": str(temp / "repo"),
                    "branch": "promptql/test",
                    "mode": "autopilot",
                    "aiClient": "notion",
                    "tunnelUrl": "https://example.trycloudflare.com",
                    "apiKey": secret,
                    "serverPid": 0,
                    "tunnelPid": 0,
                }
            ),
            encoding="utf-8",
        )
        (logs_dir / "repo-tools.jsonl").write_text(
            json.dumps({"authorization": f"Bearer {secret}", "message": secret}),
            encoding="utf-8",
        )

        output = temp / "support.zip"
        generated = admin.create_support_bundle(output)
        assert generated == output
        assert output.is_file()
        with zipfile.ZipFile(output, "r") as archive:
            names = set(archive.namelist())
            assert "summary.json" in names
            assert "config/settings.redacted.json" in names
            combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in names)
            assert secret not in combined
            assert "[REDACTED]" in combined

        report = admin.doctor_report(include_update=False)
        assert report["version"] != "unknown"
        assert any(item["name"] == "server/app_entry.py" and item["ok"] for item in report["checks"])
        status = admin.sessions()
        assert status and status[0]["id"] == "session-test"
        assert "apiKey" not in status[0]

    print("KaroX admin CLI tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
