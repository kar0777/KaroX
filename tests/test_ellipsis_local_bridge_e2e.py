from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import _path_setup
from karox.credentials import CredentialBackend
from karox.ellipsis_bridge_server import (
    EllipsisCoreBridge,
    build_ellipsis_bridge_app,
)
from karox.models import AccessProfile
from karox.remote_lease import EllipsisLeaseStore
from karox.sessions import SessionStore


class MemoryCredentialBackend(CredentialBackend):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def git(repository: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout


class EllipsisLocalBridgeE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_unpublished_local_repository_is_changed_and_verified_locally(self) -> None:
        with tempfile.TemporaryDirectory(prefix="karox-ellipsis-e2e-") as temporary:
            root = Path(temporary)
            repository = root / "private-project"
            repository.mkdir()
            git(repository, "init")
            git(repository, "config", "user.email", "test@example.invalid")
            git(repository, "config", "user.name", "KaroX Test")
            (repository / "app.py").write_text(
                "def answer():\n    return 1\n",
                encoding="utf-8",
            )
            (repository / "test_app.py").write_text(
                "import unittest\n"
                "from app import answer\n\n"
                "class TestAnswer(unittest.TestCase):\n"
                "    def test_answer(self):\n"
                "        self.assertEqual(answer(), 2)\n",
                encoding="utf-8",
            )
            git(repository, "add", "app.py", "test_app.py")
            git(repository, "commit", "-m", "initial fixture")
            self.assertEqual(git(repository, "remote", "-v").strip(), "")

            sessions = SessionStore(root / "sessions")
            local_session_id = "local-e2e-session"
            sessions.create(
                repository,
                "Change answer to two and verify it.",
                AccessProfile.WORKSPACE_WRITE,
                branch=git(repository, "branch", "--show-current").strip(),
                session_id=local_session_id,
            )
            backend = MemoryCredentialBackend()
            leases = EllipsisLeaseStore(root / "leases", backend=backend)
            lease, credential = leases.mint(
                credential_name="e2e-lease",
                local_session_id=local_session_id,
                ellipsis_session_id="ellipsis-e2e-session",
                repository=repository,
                access_profile=AccessProfile.WORKSPACE_WRITE.value,
                ttl_seconds=300,
            )
            self.assertTrue(lease.active)
            runtime = EllipsisCoreBridge(
                repository,
                sessions,
                local_session_id,
                (
                    "karox.repo.read_file",
                    "karox.repo.write_file",
                    "karox.checks.run",
                    "karox.git.status",
                    "karox.git.diff",
                    "karox.report.get",
                ),
                verification_commands=((sys.executable, "-m", "unittest", "*"),),
            )
            with patch.dict(os.environ, {"KAROX_MCP_ALLOWED_HOSTS": "testserver"}):
                app = build_ellipsis_bridge_app(
                    runtime,
                    credential_name="e2e-lease",
                    lease_store=leases,
                )
                headers = {
                    "Authorization": f"Bearer {credential}",
                    "X-KaroX-Remote-Protocol": "1",
                    "X-KaroX-Session-ID": local_session_id,
                }
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    headers=headers,
                ) as client:
                    session = await client.get("/session")
                    self.assertEqual(session.status_code, 200)
                    self.assertEqual(session.json()["session_id"], local_session_id)

                    read = await client.post(
                        "/tools/karox.repo.read_file",
                        json={"path": "app.py"},
                    )
                    self.assertEqual(read.status_code, 200, read.text)
                    self.assertIn("return 1", read.json()["data"]["content"])

                    changed_content = "def answer():\n    return 2\n"
                    write = await client.post(
                        "/tools/karox.repo.write_file",
                        json={"path": "app.py", "content": changed_content},
                        headers={"X-KaroX-Idempotency-Key": "e2e-write-1"},
                    )
                    self.assertEqual(write.status_code, 200, write.text)
                    self.assertEqual(
                        (repository / "app.py").read_text(encoding="utf-8"),
                        changed_content,
                    )

                    check = await client.post(
                        "/tools/karox.checks.run",
                        json={"argv": [sys.executable, "-m", "unittest", "-v"]},
                        headers={"X-KaroX-Idempotency-Key": "e2e-check-1"},
                    )
                    self.assertEqual(check.status_code, 200, check.text)
                    self.assertEqual(check.json()["data"]["exit_code"], 0)

                    diff = await client.post(
                        "/tools/karox.git.diff",
                        json={"paths": ["app.py"]},
                    )
                    self.assertEqual(diff.status_code, 200, diff.text)
                    self.assertIn("return 2", diff.json()["data"]["stdout"])

                    report = await client.post(
                        "/tools/karox.report.get",
                        json={},
                    )
                    self.assertEqual(report.status_code, 200, report.text)
                    self.assertIn("app.py", report.json()["data"]["changed_files"])
                    self.assertGreaterEqual(len(report.json()["data"]["checks"]), 1)

                    self.assertEqual(git(repository, "remote", "-v").strip(), "")
                    self.assertNotIn("repository", json.dumps(headers).lower())

                    leases.revoke("e2e-lease")
                    revoked = await client.get("/session")
                    self.assertEqual(revoked.status_code, 401)
                    self.assertEqual(
                        revoked.json()["error"], "credential_lease_unavailable"
                    )


if __name__ == "__main__":
    unittest.main()
