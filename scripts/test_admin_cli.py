#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def load_modules(root: Path, temp: Path):
    os.environ["HOME"] = str(temp / "home")
    os.environ["APPDATA"] = str(temp / "appdata")
    os.environ["LOCALAPPDATA"] = str(temp / "localappdata")
    os.environ["XDG_CONFIG_HOME"] = str(temp / "xdg-config")
    os.environ["XDG_DATA_HOME"] = str(temp / "xdg-data")
    scripts = root / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("karox_admin", scripts / "karox_admin.py")
    assert spec and spec.loader
    admin = importlib.util.module_from_spec(spec)
    sys.modules["karox_admin"] = admin
    spec.loader.exec_module(admin)
    support_spec = importlib.util.spec_from_file_location("support_bundle_tested", scripts / "support_bundle.py")
    assert support_spec and support_spec.loader
    support = importlib.util.module_from_spec(support_spec)
    support_spec.loader.exec_module(support)
    # Source files deliberately retain the legacy path so rebrand tests can verify
    # migration. Point the standalone stop module at that same temporary runtime.
    os.environ["KAROX_RUNTIME_DIR"] = str(admin.RUNTIME_DIR)
    stop_spec = importlib.util.spec_from_file_location("karox_stop_tested", scripts / "karox_stop.py")
    assert stop_spec and stop_spec.loader
    stop = importlib.util.module_from_spec(stop_spec)
    stop_spec.loader.exec_module(stop)
    return admin, support, stop


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="karox-admin-test-") as raw_temp:
        temp = Path(raw_temp)
        admin, support, stop = load_modules(root, temp)

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
        session_file = session_dir / "session.json"
        session_data = {
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
        session_file.write_text(json.dumps(session_data), encoding="utf-8")
        (logs_dir / "repo-tools.jsonl").write_text(
            json.dumps({"authorization": f"Bearer {secret}", "message": secret}),
            encoding="utf-8",
        )

        output = temp / "support.zip"
        generated = support.create_support_bundle(output)
        assert generated == output.resolve()
        assert output.is_file()
        with zipfile.ZipFile(output, "r") as archive:
            names = set(archive.namelist())
            assert "summary.json" in names
            assert "config/settings.redacted.json" in names
            combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in names)
            assert secret not in combined
            assert "[REDACTED" in combined
            summary = json.loads(archive.read("summary.json"))
            assert summary["privacy"]["sourceCodeIncluded"] is False
            assert summary["privacy"]["knownValuesRemoved"] == 1

        report = admin.doctor_report(include_update=False)
        assert report["version"] != "unknown"
        assert any(item["name"] == "server/app_entry.py" and item["ok"] for item in report["checks"])
        status = admin.sessions()
        assert status and status[0]["id"] == "session-test"
        assert "apiKey" not in status[0]

        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                str(stop.APP_DIR / "server" / "repo_tools.py"),
                "uvicorn",
                "repo_tools:app",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            session_data["serverPid"] = child.pid
            session_file.write_text(json.dumps(session_data), encoding="utf-8")
            deadline = time.time() + 8
            while time.time() < deadline and not stop.is_karox_process(child.pid, "server"):
                time.sleep(0.1)
            assert stop.is_karox_process(child.pid, "server")
            stopped = stop.stop_sessions("session-test")
            assert stopped["ok"], stopped
            child.wait(timeout=10)
            assert child.returncode is not None
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    print("KaroX admin CLI, safe stop, and support redaction tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
