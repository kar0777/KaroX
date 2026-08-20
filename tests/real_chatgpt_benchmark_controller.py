"""Explicit, non-discovery checks/launcher for the real ChatGPT benchmark."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "benchmarks" / "gpt_web_autonomy" / "real_chatgpt_benchmark.py"
STATE = ROOT / "scratch" / "gpt_web_real_benchmark_state.json"
REQUEST = ROOT / "scratch" / "gpt_web_real_benchmark_request.json"


def _load_controller():
    spec = importlib.util.spec_from_file_location("real_chatgpt_benchmark", CONTROLLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _wmi_launch(argv: list[str]) -> int:
    if sys.platform != "win32":
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return process.pid
    command_line = subprocess.list2cmdline(argv).replace("'", "''")
    script = (
        "$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line}'}}; "
        "if ($result.ReturnValue -ne 0) { exit $result.ReturnValue }; "
        "Write-Output $result.ProcessId"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return int(completed.stdout.strip().splitlines()[-1])


def test_controller_imports_and_declares_fair_tool_delta() -> None:
    module = _load_controller()
    baseline = set(module.BASELINE_TOOLS)
    optimized = set(module.OPTIMIZED_TOOLS)
    expected_delta = set(module.MANAGED_CHECK_TOOLS) | set(module.AUTONOMY_TOOLS)
    assert optimized - baseline == expected_delta
    assert not baseline.intersection(expected_delta)
    assert len(module.RUN_ORDER) == 22
    assert module.DEADLINE_SECONDS == 600.0


def test_all_fixture_pairs_are_identical() -> None:
    import tempfile

    module = _load_controller()
    with tempfile.TemporaryDirectory(prefix="karox-real-benchmark-fixture-smoke-") as raw:
        root = Path(raw)
        for task_id in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10"):
            pair = module._create_fixture_pair(root, task_id)
            assert pair["baseline"]["head"] == pair["optimized"]["head"]
            assert pair["baseline"]["content_digest"] == pair["optimized"]["content_digest"]
            assert pair["baseline"]["dirty_status_digest"] == pair["optimized"]["dirty_status_digest"]


def test_workspace_reset_preserves_repository_identity() -> None:
    import tempfile

    module = _load_controller()
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from karox.models import repository_fingerprint

    with tempfile.TemporaryDirectory(prefix="karox-real-benchmark-reset-smoke-") as raw:
        root = Path(raw)
        pair = module._create_fixture_pair(root, "01")
        first = module._reset_workspace(root, "baseline", "01")
        fingerprint_before = repository_fingerprint(Path(first["path"]))
        second = module._reset_workspace(root, "baseline", "01")
        fingerprint_after = repository_fingerprint(Path(second["path"]))
        assert fingerprint_after == fingerprint_before
        assert second["head"] == pair["baseline"]["head"]
        assert second["content_digest"] == pair["baseline"]["content_digest"]
        assert module._sha256_text(second["git_status"]) == pair["baseline"]["dirty_status_digest"]


def test_requested_controller_action() -> None:
    if not REQUEST.exists():
        return
    request = json.loads(REQUEST.read_text(encoding="utf-8"))
    action = request.get("action")
    if action == "prepare":
        pid = _wmi_launch([sys.executable, str(CONTROLLER), "prepare", "--state", str(STATE)])
        response = {"status": "launched", "action": action, "pid": pid}
    else:
        module = _load_controller()
        if action == "status":
            response = module._status(STATE)
        elif action == "external-status":
            response = module._external_status(STATE)
        elif action == "activate":
            response = module._activate_run(STATE, int(request["run_index"]))
        elif action == "repair-baseline-invalid":
            response = module._repair_invalid_baseline_run(STATE)
        elif action == "evaluate":
            response = module._evaluate_run(STATE)
        else:
            raise AssertionError(f"unknown requested action: {action}")
    output = ROOT / "scratch" / "gpt_web_real_benchmark_request_result.json"
    output.write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
