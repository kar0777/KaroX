#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="karox-app-test-") as raw_temp:
        temp = Path(raw_temp)
        repo = temp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)

        os.environ["REPO_ROOT"] = str(repo)
        os.environ["REPO_TOOLS_API_KEY"] = "test-session-key-abcdefghijklmnopqrstuvwxyz"
        os.environ["REPO_TOOLS_MODE"] = "read_only"
        os.environ["REPO_TOOLS_BRANCH"] = "main"
        os.environ["REPO_TOOLS_HOME"] = str(temp / "runtime")
        os.environ["REPO_TOOLS_LOG_FILE"] = str(temp / "runtime" / "audit.jsonl")
        os.environ["REPO_TOOLS_RUNS_DIR"] = str(repo / ".promptql" / "runs")
        os.environ["REPO_TOOLS_MAX_REQUEST_BYTES"] = "512"
        os.environ["REPO_TOOLS_AUDIT_MAX_BYTES"] = "1200"
        os.environ["REPO_TOOLS_DEBUG_ERRORS"] = "0"

        sys.path.insert(0, str(root / "server"))
        from fastapi.testclient import TestClient
        import app_entry

        client = TestClient(app_entry.app, raise_server_exceptions=False)
        key = os.environ["REPO_TOOLS_API_KEY"]

        control = client.get("/control")
        assert control.status_code == 200
        assert "KaroX Control Center" in control.text
        assert control.headers["x-content-type-options"] == "nosniff"
        assert control.headers["cache-control"].startswith("no-store")
        assert "x-karox-request-id" in control.headers

        unauthenticated = client.get("/capabilities")
        assert unauthenticated.status_code == 401
        wrong = client.get("/capabilities", headers={"X-API-Key": "wrong"})
        assert wrong.status_code == 401

        capabilities = client.get("/capabilities", headers={"X-API-Key": key})
        assert capabilities.status_code == 200
        caps = capabilities.json()
        assert caps["write"] is False
        assert caps["push"] is False
        assert caps["controlCenter"] is True

        meta = client.get("/meta", headers={"Authorization": f"Bearer {key}"})
        assert meta.status_code == 200
        assert meta.json()["branch"] == "main"
        assert meta.headers["x-karox-version"]

        security = client.get("/security/status", headers={"X-API-Key": key})
        assert security.status_code == 200
        assert security.json()["constantTimeKeyComparison"] is True
        assert security.json()["requestBodyLimit"] == 512

        oversized = client.post(
            "/file",
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            content='{"path":"x.txt","content":"' + ("x" * 1000) + '"}',
        )
        assert oversized.status_code == 413

        # Generate enough audit data to exercise bounded rotation.
        for _ in range(20):
            response = client.get("/meta", headers={"X-API-Key": key})
            assert response.status_code == 200
        audit = Path(os.environ["REPO_TOOLS_LOG_FILE"])
        assert audit.is_file()
        assert audit.stat().st_size < 20_000
        assert any(audit.parent.glob(audit.name + ".*"))

    print("KaroX hardened runtime tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
