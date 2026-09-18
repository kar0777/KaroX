#!/usr/bin/env python3
from __future__ import annotations

import hashlib
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


def test_support_bundle_confidentiality() -> None:
    root = Path(__file__).resolve().parents[1]
    env_name = "KAROX_SYNTHETIC_API_TOKEN"
    previous_environment = dict(os.environ)
    previous_path = list(sys.path)
    missing_module = object()
    previous_admin = sys.modules.get("karox_admin", missing_module)
    env_secret = "EnvSynthetic_Q7mZ9pL2vN8xR4cT6kW1sD5hF3jB0aY"
    known_secret = "SessionSynthetic_A9vK3mQ7xL2pR8tN5dW1zC6hF4jB0sY"
    generic_entropy = "LooseSynthetic_Q9mV2xR7kP4tN8dL5sW1cF6hJ3bZ0aY"
    raw_user_message = "RAW USER MESSAGE MUST NEVER ENTER SUPPORT DATA"
    evidence_id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    os.environ[env_name] = env_secret
    try:
        with tempfile.TemporaryDirectory(prefix="karox-support-confidentiality-") as raw_temp:
            temp = Path(raw_temp)
            admin, support, _stop = load_modules(root, temp)
            admin.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (admin.CONFIG_DIR / "settings.json").write_text(
                json.dumps(
                    {
                        "language": "en",
                        "nested": {
                            "apiKey": known_secret,
                            "headers": {
                                "Authorization": f"Bearer {known_secret}",
                                "Cookie": f"session={known_secret}",
                            },
                        },
                        "endpoint": (
                            "https://example.test/api/normal?access_token="
                            f"{known_secret}&state={env_secret}#private"
                        ),
                        "telemetryLabel": generic_entropy,
                        "customInstructions": raw_user_message,
                        "clipboard": raw_user_message,
                        "browserFormValue": raw_user_message,
                    }
                ),
                encoding="utf-8",
            )

            secretish_session_name = "SessionDir_A7mQ2xN9pR4tK8vL5sW1cF6hJ3bZ0dY"
            session_dir = admin.SESSIONS_DIR / secretish_session_name
            logs_dir = session_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            session = {
                "id": secretish_session_name,
                "mode": "build",
                "branch": "support-bundle-test",
                "aiClient": "synthetic",
                "tunnelProvider": "cloudflare",
                "startedAt": "2026-08-07 08:00:00",
                "apiKey": known_secret,
                "approvalPassword": env_secret,
                "task": raw_user_message,
                "message": raw_user_message,
                "fileContent": raw_user_message,
                "evidence_id": evidence_id,
                "nested": {
                    "exception": {
                        "type": "RuntimeError",
                        "message": f"boom {known_secret} {generic_entropy}",
                    },
                    "stdout": f"{raw_user_message} {known_secret}",
                    "stderr": f"{raw_user_message} {env_secret}",
                },
                "serverPid": 0,
                "tunnelPid": 0,
            }
            (session_dir / "session.json").write_text(json.dumps(session), encoding="utf-8")

            structured_name = f"{generic_entropy}.jsonl"
            structured_rows = [
                {
                    "ts": "2026-08-07 08:01:00",
                    "action": "repo.read_file",
                    "request_id": "req-support-1",
                    "task": raw_user_message,
                    "data": {
                        "message": raw_user_message,
                        "stdout": f"{raw_user_message} {known_secret}",
                        "authorization": f"Bearer {known_secret}",
                        "evidence_id": evidence_id,
                    },
                },
                {
                    "ts": "2026-08-07 08:01:01",
                    "action": "checks.run",
                    "status": "failed",
                    "error_code": "synthetic_failure",
                    "correlation_id": "corr-support-1",
                    "evidence_ids": [evidence_id],
                    "message": f"{raw_user_message} {env_secret}",
                },
            ]
            (logs_dir / structured_name).write_text(
                "\n".join(json.dumps(row) for row in structured_rows) + "\n",
                encoding="utf-8",
            )
            (logs_dir / f"{known_secret}.txt").write_text(
                f"{raw_user_message}\n{known_secret}\n{env_secret}\n{generic_entropy}\n",
                encoding="utf-8",
            )

            output = temp / "support-confidentiality.zip"
            generated = support.create_support_bundle(output)
            assert generated == output.resolve()
            assert output.stat().st_size <= support._MAX_BUNDLE_BYTES

            with zipfile.ZipFile(output, "r") as archive:
                names = archive.namelist()
                combined = "\n".join(
                    archive.read(name).decode("utf-8", errors="ignore") for name in names
                )
                for forbidden in (
                    known_secret,
                    env_secret,
                    generic_entropy,
                    raw_user_message,
                    secretish_session_name,
                    structured_name,
                ):
                    assert forbidden not in combined
                    assert all(forbidden not in name for name in names)
                assert evidence_id in combined
                assert "[REDACTED" in combined
                assert "REDACTED_QUERY_VALUE" in combined
                assert "sessions/session-001/session.redacted.json" in names
                assert "sessions/session-001/logs/structured-001.json" in names
                assert "sessions/session-001/logs/manifest.json" in names

                summary = json.loads(archive.read("summary.json"))
                assert summary["privacy"]["sourceCodeIncluded"] is False
                assert summary["privacy"]["rawUserMessagesIncluded"] is False
                assert summary["privacy"]["rawProcessOutputIncluded"] is False
                assert summary["privacy"]["unstructuredLogContentIncluded"] is False
                assert summary["privacy"]["knownValuesRemoved"] >= 2
                assert evidence_id in summary["evidence_ids"]

                structured = json.loads(
                    archive.read("sessions/session-001/logs/structured-001.json")
                )
                assert structured["records"]
                assert any(row.get("evidence_id") == evidence_id for row in structured["records"])
                assert all("data" not in row and "task" not in row and "message" not in row for row in structured["records"])
                manifest = json.loads(archive.read("sessions/session-001/logs/manifest.json"))
                assert any(item["kind"] == "unstructured" and not item["contentIncluded"] for item in manifest)

            # The public `karox support` admin path must route through the same
            # hardened exporter, not the legacy compatibility helper.
            cli_output = temp / "support-cli.zip"
            assert admin.main(["support", "--output", str(cli_output)]) == 0
            with zipfile.ZipFile(cli_output, "r") as cli_archive:
                cli_summary = json.loads(cli_archive.read("summary.json"))
                assert cli_summary["privacy"]["rawUserMessagesIncluded"] is False
                cli_combined = "\n".join(
                    cli_archive.read(name).decode("utf-8", errors="ignore")
                    for name in cli_archive.namelist()
                )
                assert raw_user_message not in cli_combined
                assert known_secret not in cli_combined
                assert env_secret not in cli_combined

            # Deterministic bounded fuzz/property coverage for generic credentials.
            for index in range(64):
                digest = hashlib.sha256(f"support-secret-{index}".encode()).hexdigest()
                candidate = f"Aa{index:02d}_{digest}_Z9"
                scrubbed = support.scrub_text(candidate, ())
                assert candidate not in scrubbed
                assert "[REDACTED_HIGH_ENTROPY]" in scrubbed

            # Known provider formats and nested private-content fields remain fail-closed.
            # The fixture token is concatenated at runtime: a verbatim 36-char
            # ghp_ literal would trip the CI secret-scan job even though it is
            # an inert example value.
            fixture_suffix = "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
            provider_values = (
                "ghp_" + fixture_suffix,
                "github_pat_" + fixture_suffix,
                "sk-" + fixture_suffix,
                "Bearer " + fixture_suffix,
            )
            for candidate in provider_values:
                assert candidate not in support.scrub_text(candidate, ())
            nested = support.scrub_value(
                {
                    "headers": {"Authorization": f"Bearer {known_secret}"},
                    "exceptionChain": {"message": raw_user_message},
                    "clipboard": raw_user_message,
                    "formValue": raw_user_message,
                    "stdout": raw_user_message,
                    "stderr": raw_user_message,
                    "evidence_id": evidence_id,
                },
                {known_secret},
            )
            assert nested["headers"]["Authorization"] == "[REDACTED]"
            assert nested["exceptionChain"]["message"] == "[REDACTED_PRIVATE_CONTENT]"
            assert nested["clipboard"] == "[REDACTED_PRIVATE_CONTENT]"
            assert nested["formValue"] == "[REDACTED_PRIVATE_CONTENT]"
            assert nested["stdout"] == "[REDACTED_PRIVATE_CONTENT]"
            assert nested["stderr"] == "[REDACTED_PRIVATE_CONTENT]"
            assert nested["evidence_id"] == evidence_id
    finally:
        # load_modules redirects HOME/APPDATA/XDG/runtime paths for this fixture.
        # Restore all of them, not only the synthetic token: otherwise the next
        # browser test searches a deleted temporary HOME for its Chromium cache.
        os.environ.clear()
        os.environ.update(previous_environment)
        sys.path[:] = previous_path
        if previous_admin is missing_module:
            sys.modules.pop("karox_admin", None)
        else:
            sys.modules["karox_admin"] = previous_admin


def main() -> int:
    test_support_bundle_confidentiality()
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
        # Structured logs allowlist scalar diagnostic fields only; put the
        # secret under one of them so the bundle exercises the redaction path.
        (logs_dir / "repo-tools.jsonl").write_text(
            json.dumps({"ts": "2026-09-16 10:00:00", "action": "checks.all", "status": "failed", "error_code": secret}),
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
            assert summary["privacy"]["knownValuesRemoved"] >= 1

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
