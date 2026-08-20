"""Focused tests for the B7 client-evidence recording hook.

Verifies that ``_maybe_record_client_evidence`` in ``proxy_server.py``:
* records evidence ONLY for ``runtime.status`` calls, not other tools;
* is triggered only by a real ``call_tool`` (external client), never by a
  local self-test that does ``initialize`` + ``tools/list`` only;
* stores safe fields (timestamp, tool name, success/failure) and never
  stores secrets, tokens, authorization headers, tool arguments, or file
  contents;
* is best-effort: a failure to write evidence never breaks the tool call.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.proxy_server import _maybe_record_client_evidence


class ClientEvidenceRecordingTests(unittest.TestCase):
    """_maybe_record_client_evidence records evidence for runtime.status only."""

    def test_records_evidence_for_runtime_status_success(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "KAROX_BRIDGE_DIAGNOSTICS_JSON": json.dumps({"saved_profile": "test-profile"}),
                "KAROX_VNEXT_RUNTIME_DIR": tmp,
                "KAROX_RUNTIME_DIR": tmp,
            }):
                with patch("karox.connection_status.runtime_dir", return_value=Path(tmp)):
                    _maybe_record_client_evidence("runtime.status", success=True)
                    evidence_path = Path(tmp) / "vnext" / "connection-evidence" / "test-profile.json"
                    self.assertTrue(evidence_path.exists())
                    data = json.loads(evidence_path.read_text())
                    self.assertEqual(data["last_tool_call_name"], "karox.runtime.status")
                    self.assertTrue(data["success"])
                    self.assertEqual(data["client_kind"], "mcp_external")
                    self.assertIsInstance(data["last_tool_call_at"], (int, float))

    def test_records_evidence_for_runtime_status_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "KAROX_BRIDGE_DIAGNOSTICS_JSON": json.dumps({"saved_profile": "test-profile"}),
            }):
                with patch("karox.connection_status.runtime_dir", return_value=Path(tmp)):
                    _maybe_record_client_evidence("runtime.status", success=False)
                    evidence_path = Path(tmp) / "vnext" / "connection-evidence" / "test-profile.json"
                    self.assertTrue(evidence_path.exists())
                    data = json.loads(evidence_path.read_text())
                    self.assertFalse(data["success"])

    def test_does_not_record_for_other_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "KAROX_BRIDGE_DIAGNOSTICS_JSON": json.dumps({"saved_profile": "test-profile"}),
            }):
                with patch("karox.connection_status.runtime_dir", return_value=Path(tmp)):
                    _maybe_record_client_evidence("repo.read_file", success=True)
                    evidence_path = Path(tmp) / "vnext" / "connection-evidence" / "test-profile.json"
                    self.assertFalse(evidence_path.exists())

    def test_no_secrets_in_evidence_file(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "KAROX_BRIDGE_DIAGNOSTICS_JSON": json.dumps({"saved_profile": "test-profile"}),
            }):
                with patch("karox.connection_status.runtime_dir", return_value=Path(tmp)):
                    _maybe_record_client_evidence("runtime.status", success=True)
                    evidence_path = Path(tmp) / "vnext" / "connection-evidence" / "test-profile.json"
                    raw = evidence_path.read_text().lower()
                    forbidden = ("bearer", "secret", "token", "authorization", "password", "api_key", "argument")
                    for word in forbidden:
                        self.assertNotIn(word, raw, f"evidence file contains forbidden word '{word}'")

    def test_no_diagnostics_env_does_not_crash(self) -> None:
        """If KAROX_BRIDGE_DIAGNOSTICS_JSON is unset, the hook is a no-op."""
        env = {k: v for k, v in os.environ.items() if k != "KAROX_BRIDGE_DIAGNOSTICS_JSON"}
        with patch.dict(os.environ, env, clear=True):
            # Should not raise.
            _maybe_record_client_evidence("runtime.status", success=True)

    def test_malformed_diagnostics_does_not_crash(self) -> None:
        with patch.dict(os.environ, {"KAROX_BRIDGE_DIAGNOSTICS_JSON": "not-json{"}):
            # Should not raise.
            _maybe_record_client_evidence("runtime.status", success=True)

    def test_missing_saved_profile_does_not_crash(self) -> None:
        with patch.dict(os.environ, {
            "KAROX_BRIDGE_DIAGNOSTICS_JSON": json.dumps({"other_key": "value"}),
        }):
            # Should not raise.
            _maybe_record_client_evidence("runtime.status", success=True)


if __name__ == "__main__":
    unittest.main()
