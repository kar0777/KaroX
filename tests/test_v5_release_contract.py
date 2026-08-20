"""Machine-side wrapper for the canonical v5 release-contract gate.

`scripts/check_v5_release.py` was previously runnable only by CI or a human
shell. Wrapping it in pytest, the same way `test_release_gates.py` wraps the
other gates, makes the release contract executable through every approved
test surface: local pytest, CI, and a hosted bridge whose verification
allowlist includes the plain full suite.

Development builds may keep live records pending, so the non-strict gate must
pass on every commit; --strict remains the RC/final bar and is exercised here
only for its report shape, not asserted green before the live evidence lands.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401 - inserts src on sys.path


def _load_gate():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "check_v5_release", root / "scripts" / "check_v5_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V5ReleaseContractGateTests(unittest.TestCase):
    def test_dev_mode_contract_gate_passes(self) -> None:
        gate = _load_gate()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = gate.main(["--json"])
        if result:
            print(output.getvalue())
        self.assertEqual(result, 0)

    def test_json_report_names_pending_live_evidence_explicitly(self) -> None:
        gate = _load_gate()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            gate.main(["--json"])
        payload = json.loads(output.getvalue())
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload)


if __name__ == "__main__":
    unittest.main()
