"""Real paired ChatGPT Web benchmark controller.

This module is deliberately separate from the deterministic simulated-client
benchmark.  It prepares two isolated OAuth/MCP bridges backed by disposable Git
fixtures, records content-free protocol/tool-call metadata, and exposes bounded
controller actions used between fresh ChatGPT Web runs.

It never edits, restarts, migrates, or reuses the live ``clickup-opus`` profile.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
SCRATCH_STATE = ROOT / "scratch" / "gpt_web_real_benchmark_state.json"
SCRATCH_NEXT = ROOT / "scratch" / "gpt_web_real_benchmark_next.json"
SCHEMA_VERSION = 1
DEADLINE_SECONDS = 600.0
DEV_SERVER_PORT = 18765
LOCAL_PROBE_CLIENT = "karox-benchmark-local-probe"

BASELINE_PROFILE = "chatgpt-benchmark-baseline"
OPTIMIZED_PROFILE = "chatgpt-benchmark-optimized"
DISPLAY_NAMES = {
    "baseline": "KaroX Benchmark Baseline",
    "optimized": "KaroX Benchmark Optimized",
}
PROFILE_NAMES = {
    "baseline": BASELINE_PROFILE,
    "optimized": OPTIMIZED_PROFILE,
}

CORE_BASELINE_TOOLS = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.repo.edit_file",
    "karox.repo.write_file",
    "karox.repo.command",
    "karox.checks.run",
    "karox.tests.run",
    "karox.runtime.status",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
)
HOSTED_BASELINE_TOOLS = (
    "karox.artifact.get",
    "karox.artifact.read_image",
    "karox.browser.command",
    "karox.browser.open",
    "karox.browser.tabs",
    "karox.browser.new_tab",
    "karox.browser.switch_tab",
    "karox.browser.close_tab",
    "karox.browser.snapshot",
    "karox.browser.click",
    "karox.browser.fill",
    "karox.browser.select",
    "karox.browser.press",
    "karox.browser.wait_for",
    "karox.browser.get_text",
    "karox.browser.screenshot",
    "karox.browser.console",
    "karox.browser.network_failures",
    "karox.dev_server.start",
    "karox.dev_server.status",
    "karox.dev_server.logs",
    "karox.dev_server.stop",
)
MANAGED_CHECK_TOOLS = (
    "karox.checks.start",
    "karox.checks.status",
    "karox.checks.logs",
    "karox.checks.cancel",
)
AUTONOMY_TOOLS = (
    "karox.task.bootstrap",
    "karox.task.checkpoint",
    "karox.task.resume",
    "karox.task.status",
    "karox.repo.inspect",
    "karox.task.execute_plan",
    "karox.checks.run_affected",
)
BASELINE_TOOLS = CORE_BASELINE_TOOLS + HOSTED_BASELINE_TOOLS
OPTIMIZED_TOOLS = BASELINE_TOOLS + MANAGED_CHECK_TOOLS + AUTONOMY_TOOLS

VERIFICATION_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pytest", "tests/test_validation.py"),
    ("python", "-m", "pytest", "tests/test_storage.py"),
    ("python", "-m", "pytest", "tests/test_service.py", "tests/test_runtime.py"),
    ("python", "-m", "pytest", "tests/test_refactor.py"),
    ("python", "-m", "pytest", "tests/test_ui_contract.py"),
    ("python", "-m", "pytest"),
    ("python", "scripts/long_verify.py"),
)

TASKS: dict[str, dict[str, str]] = {
    "01": {
        "title": "Repository orientation",
        "prompt": (
            "Work only inside the connected benchmark repository. Find the production "
            "implementation that normalizes connector names and identify the tests that "
            "directly cover it. Do not modify production code or tests. Create "
            "benchmark/answer.json containing implementation_file, symbol, test_files "
            "(an array), and one_sentence_behavior. Verify the paths you report exist."
        ),
    },
    "02": {
        "title": "Call-flow investigation",
        "prompt": (
            "Work only inside the connected benchmark repository. Trace the runtime call "
            "flow initiated when the TUI connect screen submits a connector name, through "
            "the service layer and supervisor, until the worker runtime starts. Do not "
            "modify production code or tests. Create benchmark/answer.json with a flow "
            "array of fully qualified Class.method or function symbols in execution order, "
            "plus one short note describing where validation happens."
        ),
    },
    "03": {
        "title": "Bug diagnosis",
        "prompt": (
            "A deterministic focused test in this repository is failing. Diagnose the root "
            "cause, but do not fix production code or tests. Run only the verification you "
            "need. Create benchmark/diagnosis.json with failing_test, root_cause_symbol, "
            "root_cause_code, and explanation. The diagnosis must be specific enough for "
            "another engineer to implement the fix without redoing the investigation."
        ),
    },
    "04": {
        "title": "Small scoped fix",
        "prompt": (
            "Connector names must not contain forward slashes or backslashes. Implement "
            "that validation rule in the existing normalization path, add focused regression "
            "tests, and run the smallest sufficient verification. Preserve all existing "
            "behavior for valid names. Do not make unrelated changes."
        ),
    },
    "05": {
        "title": "Multi-file change",
        "prompt": (
            "Add an optional timeout_seconds field to the connection request and propagate "
            "it through the service into the runtime supervisor/worker launch path. The "
            "existing behavior must remain unchanged when the field is omitted. Update the "
            "relevant tests and run sufficient focused verification. Avoid unrelated edits."
        ),
    },
    "06": {
        "title": "Medium refactor",
        "prompt": (
            "Refactor the duplicated status formatting logic in the production service and "
            "runtime modules into one shared production helper without changing observable "
            "behavior. Update imports/tests as needed and verify the refactor. Keep the diff "
            "focused and do not add a compatibility copy of the old implementation."
        ),
    },
    "07": {
        "title": "Affected verification",
        "prompt": (
            "This benchmark repository intentionally starts with a small dirty change. "
            "Inspect the existing diff, determine the smallest sufficient verification for "
            "that change, run it, and create benchmark/verification.json with commands "
            "(an array of argv arrays), rationale, and result. Do not alter production code "
            "or tests beyond the dirty state that was already present when you started."
        ),
    },
    "08": {
        "title": "Long-running verification",
        "prompt": (
            "Run the repository's designated long verification script and establish whether "
            "it completes successfully. Do not modify production code or tests. Create "
            "benchmark/long_verification.json with command, success, and completion_marker. "
            "Use the connected tool surface as you judge appropriate and do not ask me to "
            "run the command manually."
        ),
    },
    "09": {
        "title": "Dev-server/browser task",
        "prompt": (
            "Start the repository's approved local benchmark web app, verify its readiness, "
            "inspect the rendered UI through the connected managed browser, and create "
            "benchmark/ui_observation.json with title, heading, status_text, and "
            "connect_button_present. Do not use a personal browser and do not access any "
            "external website. Stop at observation; do not submit forms."
        ),
    },
    "10a": {
        "title": "Cross-chat recovery — checkpoint",
        "prompt": (
            "Investigate the repository's reconnect backoff policy and determine the change "
            "required by the documented reconnect requirement. Do not edit production code "
            "yet. Persist enough durable progress using facilities available through the "
            "connected repository so that a completely fresh chat can continue without me "
            "restating the investigation, then stop after confirming the durable recovery "
            "state exists."
        ),
    },
    "10b": {
        "title": "Cross-chat recovery — resume",
        "prompt": (
            "Continue the previously checkpointed reconnect-policy task from durable state. "
            "Do not ask me to restate details. Implement the required reconnect backoff cap "
            "with focused tests and run sufficient verification. Keep the change scoped to "
            "the documented policy."
        ),
    },
}

RUN_ORDER: tuple[tuple[str, str], ...] = (
    ("01", "baseline"), ("01", "optimized"),
    ("02", "optimized"), ("02", "baseline"),
    ("03", "baseline"), ("03", "optimized"),
    ("04", "optimized"), ("04", "baseline"),
    ("05", "baseline"), ("05", "optimized"),
    ("06", "optimized"), ("06", "baseline"),
    ("07", "baseline"), ("07", "optimized"),
    ("08", "optimized"), ("08", "baseline"),
    ("09", "baseline"), ("09", "optimized"),
    ("10a", "optimized"), ("10b", "optimized"),
    ("10a", "baseline"), ("10b", "baseline"),
)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def _json_load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _run(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> str:
    completed = subprocess.run(
        list(argv), cwd=cwd, env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n"
            f"{completed.stderr[-1200:]}"
        )
    return completed.stdout.strip()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _wait_port(port: int, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"local benchmark bridge did not open port {port}")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if ".git" in rel.parts or "__pycache__" in rel.parts or ".pytest_cache" in rel.parts:
            continue
        if rel.parts and rel.parts[0] == "benchmark":
            continue
        files.append(path)
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_head(repository: Path) -> str:
    return _run(("git", "rev-parse", "HEAD"), cwd=repository)


def _git_status(repository: Path) -> str:
    return _run(("git", "status", "--porcelain=v1"), cwd=repository)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def make_writable(function: Any, target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=make_writable)


def _create_git_head(repository: Path) -> str:
    """Create a synthetic fixture HEAD without invoking ``git commit``."""
    _run(("git", "init", "--quiet", "--initial-branch=main"), cwd=repository)
    _run(("git", "add", "-A"), cwd=repository)
    tree = _run(("git", "write-tree"), cwd=repository)
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "KaroX Benchmark",
            "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
            "GIT_COMMITTER_NAME": "KaroX Benchmark",
            "GIT_COMMITTER_EMAIL": "benchmark@example.invalid",
            "GIT_AUTHOR_DATE": "2026-08-07T08:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-07T08:00:00+00:00",
        }
    )
    commit = subprocess.run(
        ("git", "commit-tree", tree, "-m", "benchmark fixture"), cwd=repository,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"cannot create fixture HEAD: {commit.stderr[-1000:]}")
    sha = commit.stdout.strip()
    _run(("git", "update-ref", "refs/heads/main", sha), cwd=repository)
    _run(("git", "symbolic-ref", "HEAD", "refs/heads/main"), cwd=repository)
    return sha


def _base_fixture_files(*, task_id: str) -> dict[str, str]:
    lease_impl = "return now > expires_at" if task_id == "03" else "return now >= expires_at"
    return {
        ".gitignore": ".pytest_cache/\n__pycache__/\n*.pyc\n",
        "pyproject.toml": "[project]\nname='relaydesk-benchmark'\nversion='0.0.0'\n",
        "src/relaydesk/__init__.py": "__all__ = []\n",
        "src/relaydesk/validation.py": (
            "def normalize_connector_name(value: str) -> str:\n"
            "    clean = value.strip()\n"
            "    if not clean:\n"
            "        raise ValueError('connector name is required')\n"
            "    return clean\n"
        ),
        "src/relaydesk/models.py": (
            "from dataclasses import dataclass\n\n"
            "@dataclass(frozen=True)\n"
            "class ConnectionRequest:\n"
            "    name: str\n"
        ),
        "src/relaydesk/runtime.py": (
            "from .models import ConnectionRequest\n\n"
            "def format_status(name: str, state: str) -> str:\n"
            "    return f'{name}: {state}'\n\n"
            "class WorkerRuntime:\n"
            "    def start(self, request: ConnectionRequest) -> dict[str, object]:\n"
            "        return {'name': request.name, 'started': True, 'timeout_seconds': 15}\n\n"
            "class RuntimeSupervisor:\n"
            "    def __init__(self, worker: WorkerRuntime | None = None) -> None:\n"
            "        self.worker = worker or WorkerRuntime()\n\n"
            "    def launch(self, request: ConnectionRequest) -> dict[str, object]:\n"
            "        return self.worker.start(request)\n"
        ),
        "src/relaydesk/service.py": (
            "from .models import ConnectionRequest\n"
            "from .runtime import RuntimeSupervisor\n\n"
            "def format_status(name: str, state: str) -> str:\n"
            "    return f'{name}: {state}'\n\n"
            "class ConnectionService:\n"
            "    def __init__(self, supervisor: RuntimeSupervisor | None = None) -> None:\n"
            "        self.supervisor = supervisor or RuntimeSupervisor()\n\n"
            "    def connect(self, request: ConnectionRequest) -> dict[str, object]:\n"
            "        return self.supervisor.launch(request)\n"
        ),
        "src/relaydesk/tui.py": (
            "from .models import ConnectionRequest\n"
            "from .service import ConnectionService\n"
            "from .validation import normalize_connector_name\n\n"
            "class ConnectScreen:\n"
            "    def __init__(self, service: ConnectionService | None = None) -> None:\n"
            "        self.service = service or ConnectionService()\n\n"
            "    def submit(self, value: str) -> dict[str, object]:\n"
            "        name = normalize_connector_name(value)\n"
            "        return self.service.connect(ConnectionRequest(name=name))\n"
        ),
        "src/relaydesk/lease.py": (
            "def lease_expired(expires_at: float, now: float) -> bool:\n"
            f"    {lease_impl}\n"
        ),
        "src/relaydesk/storage.py": (
            "class MemoryStore:\n"
            "    def __init__(self) -> None:\n"
            "        self._items: dict[str, str] = {}\n\n"
            "    def put(self, key: str, value: str) -> None:\n"
            "        self._items[key] = value\n\n"
            "    def get(self, key: str) -> str | None:\n"
            "        return self._items.get(key)\n"
        ),
        "src/relaydesk/registry.py": (
            "KNOWN_CONNECTORS = {'chatgpt', 'claude', 'local'}\n\n"
            "def known_connector(name: str) -> bool:\n"
            "    return name in KNOWN_CONNECTORS\n"
        ),
        "src/relaydesk/reconnect.py": (
            "def reconnect_delay(attempt: int) -> int:\n"
            "    if attempt < 0:\n"
            "        raise ValueError('attempt must be non-negative')\n"
            "    return 2 ** attempt\n"
        ),
        "tests/conftest.py": (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
        ),
        "tests/test_validation.py": (
            "import pytest\nfrom relaydesk.validation import normalize_connector_name\n\n"
            "def test_normalizes_whitespace():\n"
            "    assert normalize_connector_name(' demo ') == 'demo'\n\n"
            "def test_rejects_empty():\n"
            "    with pytest.raises(ValueError):\n"
            "        normalize_connector_name('   ')\n"
        ),
        "tests/test_service.py": (
            "from relaydesk.models import ConnectionRequest\n"
            "from relaydesk.service import ConnectionService\n\n"
            "def test_service_launches_request():\n"
            "    result = ConnectionService().connect(ConnectionRequest('demo'))\n"
            "    assert result['name'] == 'demo'\n"
            "    assert result['started'] is True\n"
        ),
        "tests/test_runtime.py": (
            "from relaydesk.models import ConnectionRequest\n"
            "from relaydesk.runtime import RuntimeSupervisor, WorkerRuntime\n\n"
            "def test_supervisor_uses_worker():\n"
            "    result = RuntimeSupervisor(WorkerRuntime()).launch(ConnectionRequest('demo'))\n"
            "    assert result['timeout_seconds'] == 15\n"
        ),
        "tests/test_tui.py": (
            "from relaydesk.tui import ConnectScreen\n\n"
            "def test_connect_flow():\n"
            "    assert ConnectScreen().submit(' demo ')['name'] == 'demo'\n"
        ),
        "tests/test_lease.py": (
            "from relaydesk.lease import lease_expired\n\n"
            "def test_lease_expires_at_boundary():\n"
            "    assert lease_expired(10.0, 10.0) is True\n"
        ),
        "tests/test_storage.py": (
            "from relaydesk.storage import MemoryStore\n\n"
            "def test_round_trip():\n"
            "    store = MemoryStore(); store.put('a', 'b'); assert store.get('a') == 'b'\n"
        ),
        "tests/test_refactor.py": (
            "from relaydesk.runtime import format_status as runtime_status\n"
            "from relaydesk.service import format_status as service_status\n\n"
            "def test_status_formatting_matches():\n"
            "    assert runtime_status('demo', 'ready') == 'demo: ready'\n"
            "    assert service_status('demo', 'ready') == 'demo: ready'\n"
        ),
        "tests/test_reconnect.py": (
            "from relaydesk.reconnect import reconnect_delay\n\n"
            "def test_backoff_starts_exponentially():\n"
            "    assert [reconnect_delay(i) for i in range(4)] == [1, 2, 4, 8]\n"
        ),
        "tests/test_ui_contract.py": (
            "from pathlib import Path\n\n"
            "def test_ui_fixture_contains_expected_controls():\n"
            "    text = Path('app/index.html').read_text(encoding='utf-8')\n"
            "    assert 'Connector Console' in text and 'Connect' in text\n"
        ),
        "docs/architecture.md": (
            "# RelayDesk architecture\n\n"
            "The TUI ConnectScreen delegates to ConnectionService, which sends a "
            "ConnectionRequest to RuntimeSupervisor and WorkerRuntime. Validation is "
            "performed before the service call. Storage and registry modules are separate.\n"
        ),
        "docs/reconnect-policy.md": (
            "# Reconnect policy\n\nReconnect delay is exponential for early attempts but must never exceed 30 seconds.\n"
        ),
        "docs/operations.md": (
            "# Operations\n\nUse the approved local benchmark server only. External endpoints are not needed.\n"
        ),
        "app/index.html": (
            "<!doctype html><html><head><title>RelayDesk Benchmark</title></head>"
            "<body><h1>Connector Console</h1><p id='status'>ready</p>"
            "<button id='connect'>Connect</button></body></html>\n"
        ),
        "benchmark_server.py": (
            "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
            "from pathlib import Path\n"
            f"PORT = {DEV_SERVER_PORT}\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def log_message(self, format, *args):\n        return\n"
            "    def do_GET(self):\n"
            "        if self.path == '/health':\n"
            "            body = b'{\"status\":\"ok\"}'\n"
            "            self.send_response(200); self.send_header('Content-Type','application/json'); "
            "self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return\n"
            "        body = Path('app/index.html').read_bytes()\n"
            "        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); "
            "self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)\n"
            "ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()\n"
        ),
        "scripts/long_verify.py": (
            "import time\nfrom pathlib import Path\n"
            "print('LONG_VERIFY_STARTED', flush=True)\n"
            "time.sleep(35)\n"
            "Path('benchmark').mkdir(exist_ok=True)\n"
            "Path('benchmark/long_verify.done').write_text('LONG_VERIFY_COMPLETED\\n', encoding='utf-8')\n"
            "print('LONG_VERIFY_COMPLETED', flush=True)\n"
        ),
    }


def _create_fixture_pair(root: Path, task_id: str) -> dict[str, Any]:
    template = root / "templates" / f"task-{task_id}"
    template.mkdir(parents=True, exist_ok=True)
    for rel, text in _base_fixture_files(task_id=task_id).items():
        _write(template / rel, text)
    head = _create_git_head(template)

    pair: dict[str, Any] = {"head": head}
    for side in ("baseline", "optimized"):
        target = root / "fixtures" / f"task-{task_id}" / side
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template, target)
        if task_id == "07":
            validation = target / "src" / "relaydesk" / "validation.py"
            original = validation.read_text(encoding="utf-8")
            validation.write_text(
                original.replace(
                    "    return clean\n",
                    "    if len(clean) > 32:\n        raise ValueError('connector name is too long')\n    return clean\n",
                ),
                encoding="utf-8",
                newline="\n",
            )
            test = target / "tests" / "test_validation.py"
            test.write_text(
                test.read_text(encoding="utf-8")
                + "\ndef test_rejects_name_over_32_characters():\n"
                  "    import pytest\n"
                  "    with pytest.raises(ValueError):\n"
                  "        normalize_connector_name('x' * 33)\n",
                encoding="utf-8",
                newline="\n",
            )
        pair[side] = {
            "path": str(target),
            "head": _git_head(target),
            "content_digest": _tree_digest(target),
            "dirty_status_digest": _sha256_text(_git_status(target)),
        }
    if pair["baseline"]["head"] != pair["optimized"]["head"]:
        raise RuntimeError(f"task {task_id}: fixture HEAD mismatch")
    if pair["baseline"]["content_digest"] != pair["optimized"]["content_digest"]:
        raise RuntimeError(f"task {task_id}: fixture content digest mismatch")
    if pair["baseline"]["dirty_status_digest"] != pair["optimized"]["dirty_status_digest"]:
        raise RuntimeError(f"task {task_id}: fixture dirty-state mismatch")
    # Keep the template as an immutable source snapshot.  On Windows Git object
    # files may be read-only, and deleting the template here adds no benchmark
    # isolation value while making cleanup platform-dependent.
    return pair


def _reset_workspace(root: Path, side: str, task_id: str, *, preserve: bool = False) -> dict[str, Any]:
    workspace = root / "workspaces" / side
    if not preserve:
        source_task = "10" if task_id.startswith("10") else task_id
        source = root / "fixtures" / f"task-{source_task}" / side
        if not workspace.exists():
            shutil.copytree(source, workspace)
        else:
            # A KaroX session is bound to repository identity, including the
            # existing .git marker's filesystem identity. Replacing the whole
            # workspace here invalidates that binding and makes every later MCP
            # call fail closed as a session-policy denial. Reset only disposable
            # worktree content while preserving the already-bound .git marker.
            git_marker = workspace / ".git"
            if not git_marker.exists():
                raise RuntimeError("benchmark workspace lost its .git identity")
            before = git_marker.stat()
            for child in workspace.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir() and not child.is_symlink():
                    _remove_tree(child)
                else:
                    child.unlink()
            for child in source.iterdir():
                if child.name == ".git":
                    continue
                target = workspace / child.name
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)
            source_head = _git_head(source)
            _run(("git", "fetch", "--quiet", "--no-tags", "--force", str(source), source_head), cwd=workspace)
            _run(("git", "reset", "--quiet", "--mixed", source_head), cwd=workspace)
            after = git_marker.stat()
            if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
                raise RuntimeError("benchmark reset changed repository identity")
    return {
        "path": str(workspace),
        "head": _git_head(workspace),
        "content_digest": _tree_digest(workspace),
        "git_status": _git_status(workspace),
    }


def _tool_names_digest(tools: Iterable[str]) -> str:
    return _sha256_text("\n".join(sorted(tools)))


def _descriptor_digest(tools: Sequence[Any]) -> str:
    serial: list[dict[str, Any]] = []
    for tool in tools:
        if hasattr(tool, "model_dump"):
            value = tool.model_dump(mode="json")
        elif isinstance(tool, Mapping):
            value = dict(tool)
        else:
            value = {"name": str(getattr(tool, "name", ""))}
        serial.append(value)
    serial.sort(key=lambda item: str(item.get("name", "")))
    return _sha256_text(json.dumps(serial, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _powershell_copy_helper(*, root: Path, side: str, profile: str) -> str:
    side_root = root / "side" / side
    python = Path(sys.executable)
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$env:KAROX_VNEXT_CONFIG_DIR = '{str(side_root / 'config').replace("'", "''")}'",
        f"$env:KAROX_VNEXT_RUNTIME_DIR = '{str(side_root / 'runtime').replace("'", "''")}'",
        f"$env:PYTHONPATH = '{str(SRC).replace("'", "''")}'",
        f"& '{str(python).replace("'", "''")}' -m karox.cli bridge oauth approval-password --saved '{profile}' --copy --quiet | Out-Null",
        "if ($LASTEXITCODE -ne 0) { throw 'Could not copy OAuth approval password' }",
        "Write-Host 'OAuth approval password copied to clipboard; KaroX will auto-clear it.'",
    ]
    return "\n".join(lines) + "\n"


def _wmi_launch(argv: Sequence[str], *, cwd: Path) -> int:
    if os.name != "nt":
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return int(process.pid)
    command_line = subprocess.list2cmdline(list(argv)).replace("'", "''")
    script = (
        "$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line}'}}; "
        "if ($result.ReturnValue -ne 0) { exit $result.ReturnValue }; "
        "Write-Output $result.ProcessId"
    )
    completed = subprocess.run(
        ("powershell", "-NoProfile", "-NonInteractive", "-Command", script),
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"WMI launch failed: {completed.stderr[-1000:]}")
    return int(completed.stdout.strip().splitlines()[-1])


def _side_env(root: Path, side: str) -> dict[str, str]:
    side_root = root / "side" / side
    env = dict(os.environ)
    env["KAROX_VNEXT_CONFIG_DIR"] = str(side_root / "config")
    env["KAROX_VNEXT_RUNTIME_DIR"] = str(side_root / "runtime")
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _prepare(state_path: Path) -> int:
    existing = _json_load(state_path, {})
    if isinstance(existing, dict) and existing.get("status") == "ready":
        raise RuntimeError("a prepared real ChatGPT benchmark already exists; do not overwrite it")

    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "KaroX-Benchmarks"
    benchmark_id = f"gpt-web-autonomy-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    bench_root = (base / benchmark_id).resolve()
    bench_root.mkdir(parents=True)
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "status": "preparing",
        "root": str(bench_root),
        "created_at": time.time(),
        "protocol": "benchmarks/gpt_web_autonomy/PAIRED_CHATGPT_WEB_PROTOCOL.md",
        "main_repository": str(ROOT),
        "main_repository_mutated_as_fixture": False,
        "live_profile": "clickup-opus",
        "live_profile_used": False,
        "fixtures": {},
        "sides": {},
        "run_order": [],
    }
    _atomic_json(state_path, state)
    try:
        for task_id in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10"):
            state["fixtures"][task_id] = _create_fixture_pair(bench_root, task_id)
            _atomic_json(state_path, state)

        initial: dict[str, Any] = {}
        for side in ("baseline", "optimized"):
            initial[side] = _reset_workspace(bench_root, side, "01")
        if initial["baseline"]["head"] != initial["optimized"]["head"]:
            raise RuntimeError("initial workspace HEAD mismatch")
        if initial["baseline"]["content_digest"] != initial["optimized"]["content_digest"]:
            raise RuntimeError("initial workspace content mismatch")

        for index, (task_id, side) in enumerate(RUN_ORDER, start=1):
            prompt = TASKS[task_id]["prompt"]
            state["run_order"].append(
                {
                    "run_index": index,
                    "task_id": task_id,
                    "variant": side,
                    "title": TASKS[task_id]["title"],
                    "prompt_sha256": _sha256_text(prompt),
                }
            )

        ports = {"baseline": _free_port(), "optimized": _free_port()}
        if ports["baseline"] == ports["optimized"]:
            ports["optimized"] = _free_port()
        for side in ("baseline", "optimized"):
            side_root = bench_root / "side" / side
            (side_root / "config").mkdir(parents=True)
            (side_root / "runtime").mkdir(parents=True)
            (side_root / "control").mkdir(parents=True)
            (side_root / "events").mkdir(parents=True)
            side_state = side_root / "ready.json"
            pid = _wmi_launch(
                (
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "serve-side",
                    "--root", str(bench_root),
                    "--side", side,
                    "--port", str(ports[side]),
                    "--state", str(side_state),
                ),
                cwd=ROOT,
            )
            state["sides"][side] = {
                "launcher_pid": pid,
                "state_path": str(side_state),
                "profile": PROFILE_NAMES[side],
                "display_name": DISPLAY_NAMES[side],
                "port": ports[side],
                "workspace": initial[side],
            }
            _atomic_json(state_path, state)

        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            ready = True
            for side in ("baseline", "optimized"):
                side_payload = _json_load(Path(state["sides"][side]["state_path"]), {})
                if isinstance(side_payload, dict) and side_payload.get("status") == "error":
                    raise RuntimeError(f"{side} bridge preparation failed: {side_payload.get('error')}")
                if not isinstance(side_payload, dict) or side_payload.get("status") != "ready":
                    ready = False
            if ready:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("benchmark side bridges did not become ready")

        for side in ("baseline", "optimized"):
            state["sides"][side].update(_json_load(Path(state["sides"][side]["state_path"]), {}))

        baseline = state["sides"]["baseline"]
        optimized = state["sides"]["optimized"]
        distinct_fields = ("session_id", "port", "credential_reference", "runtime_root", "config_root", "mcp_url")
        for field in distinct_fields:
            if baseline.get(field) == optimized.get(field):
                raise RuntimeError(f"benchmark isolation failure: {field} is shared")
        if baseline.get("access_profile") != optimized.get("access_profile"):
            raise RuntimeError("benchmark fairness failure: access profiles differ")
        if baseline.get("deadline_seconds") != optimized.get("deadline_seconds"):
            raise RuntimeError("benchmark fairness failure: deadlines differ")
        if baseline.get("verification_commands") != optimized.get("verification_commands"):
            raise RuntimeError("benchmark fairness failure: verification allowlists differ")
        if baseline.get("browser_policy") != optimized.get("browser_policy"):
            raise RuntimeError("benchmark fairness failure: browser policies differ")
        if baseline.get("server_profiles") != optimized.get("server_profiles"):
            raise RuntimeError("benchmark fairness failure: server profiles differ")

        baseline_tools = set(baseline.get("tools", []))
        optimized_tools = set(optimized.get("tools", []))
        if baseline_tools != set(BASELINE_TOOLS):
            raise RuntimeError("baseline tool list differs from the predeclared low-level set")
        if optimized_tools != set(OPTIMIZED_TOOLS):
            raise RuntimeError("optimized tool list differs from the predeclared optimized set")
        forbidden = set(AUTONOMY_TOOLS) | set(MANAGED_CHECK_TOOLS)
        if baseline_tools.intersection(forbidden):
            raise RuntimeError("baseline leaked optimized-only tools")
        if optimized_tools - baseline_tools != forbidden:
            raise RuntimeError("optimized side has permissions/tools beyond the declared delta")

        for side in ("baseline", "optimized"):
            helper = ROOT / "scratch" / f"copy_benchmark_{side}_oauth.ps1"
            helper.write_text(
                _powershell_copy_helper(root=bench_root, side=side, profile=PROFILE_NAMES[side]),
                encoding="utf-8", newline="\n",
            )
            state["sides"][side]["oauth_copy_helper"] = str(helper)
        state["status"] = "ready"
        state["prepared_at"] = time.time()
        state["next_run_index"] = 1
        state["benchmark_started"] = False
        _atomic_json(state_path, state)
        return 0
    except Exception as exc:
        state["status"] = "error"
        state["error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(state_path, state)
        raise


@dataclass
class _CallRecorder:
    path: Path
    salt: bytes

    def record(self, *, tool: str, arguments: Mapping[str, Any], started: float, success: bool, error_code: str | None) -> None:
        active = _json_load(self.path.parent.parent / "control" / "active-run.json", {})
        if not isinstance(active, dict) or not active.get("run_id"):
            return
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        input_fp = hashlib.sha256(self.salt + canonical.encode("utf-8")).hexdigest()
        resource_fp = None
        if tool in {"karox.repo.read_file", "karox.repo.read_lines"}:
            value = arguments.get("path")
            if isinstance(value, str):
                resource_fp = hashlib.sha256(self.salt + value.encode("utf-8")).hexdigest()
        event = {
            "schema_version": 1,
            "run_id": active["run_id"],
            "task_id": active["task_id"],
            "variant": active["variant"],
            "ts": time.time(),
            "tool": tool,
            "input_size_bytes": len(canonical.encode("utf-8")),
            "input_fingerprint": input_fp,
            "resource_fingerprint": resource_fp,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "success": success,
            "error_code": error_code,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


class _RecordingRuntime:
    def __init__(self, runtime: Any, recorder: _CallRecorder) -> None:
        self.runtime = runtime
        self.recorder = recorder

    def descriptors(self) -> list[Any]:
        return self.runtime.descriptors()

    def session_info(self) -> dict[str, Any]:
        reader = getattr(self.runtime, "session_info", None)
        return dict(reader()) if callable(reader) else {}

    def execute(self, tool_name: str, arguments: dict[str, Any], *, idempotency_key: str | None = None, deadline_seconds: float = DEADLINE_SECONDS) -> dict[str, Any]:
        started = time.perf_counter()
        success = False
        error_code: str | None = None
        try:
            result = self.runtime.execute(
                tool_name, arguments, idempotency_key=idempotency_key,
                deadline_seconds=deadline_seconds,
            )
            if isinstance(result, Mapping):
                success = result.get("ok") is not False
                raw_code = result.get("error_code")
                error_code = str(raw_code) if raw_code else None
            else:
                success = True
            return result
        except Exception as exc:
            error_code = type(exc).__name__
            raise
        finally:
            self.recorder.record(
                tool=tool_name, arguments=arguments, started=started,
                success=success, error_code=error_code,
            )


class _ProtocolMetadataMiddleware:
    def __init__(self, app: Any, path: Path) -> None:
        self.app = app
        self.path = path
        self._lock = threading.Lock()

    def _record(self, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            method = item.get("method")
            if method not in {"initialize", "tools/list"}:
                continue
            event: dict[str, Any] = {
                "schema_version": 1,
                "ts": time.time(),
                "method": method,
            }
            if method == "initialize":
                params = item.get("params")
                info = params.get("clientInfo") if isinstance(params, dict) else None
                if isinstance(info, dict):
                    event["client_name"] = str(info.get("name") or "")[:120]
                    event["client_version"] = str(info.get("version") or "")[:120]
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/mcp" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []

        async def wrapped_receive() -> dict[str, Any]:
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes) and sum(map(len, chunks)) < 64_000:
                    chunks.append(body[: 64_000 - sum(map(len, chunks))])
                if not message.get("more_body", False):
                    self._record(b"".join(chunks))
            return message

        await self.app(scope, wrapped_receive, send)


def _tool_error_code(result: Any) -> str | None:
    value = getattr(result, "structuredContent", None)
    if isinstance(value, Mapping):
        raw = value.get("error_code")
        if raw:
            return str(raw)
    return None


def _serve_side(root: Path, side: str, port: int, state_path: Path) -> int:
    if side not in {"baseline", "optimized"}:
        raise ValueError("side must be baseline or optimized")
    side_root = root / "side" / side
    os.environ.update(_side_env(root, side))
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    state: dict[str, Any] = {
        "schema_version": 1,
        "status": "starting",
        "side": side,
        "profile": PROFILE_NAMES[side],
        "port": port,
        "pid": os.getpid(),
    }
    _atomic_json(state_path, state)
    tunnel = None
    server = None
    hosted_runtime = None
    try:
        import anyio
        import httpx
        import uvicorn
        from datetime import timedelta
        from mcp import ClientSession
        from karox.artifacts import ArtifactStore
        from karox.autonomy_runtime import AutonomyRuntime
        from karox.bridge import BridgeCredentialStore
        from karox.browser_access import BrowserAccessPolicy
        from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge
        from karox.hosted_tools_runtime import HostedToolsRuntime, ManagedServerProfile
        from karox.mcp_client import streamable_http_transport
        from karox.models import AccessProfile, Origin, OriginKind
        from karox.oauth_bridge import build_oauth_proxy_asgi_app
        from karox.paths import oauth_state_dir, runtime_dir, session_dir, config_dir
        from karox.proxy_server import wire_tool_name
        from karox.sessions import SessionStore
        from karox.web_bridge_launcher import (
            _create_child_job, _adopt_child, saved_web_bridge_session_id,
            start_cloudflare_quick_tunnel,
        )
        from karox.web_bridge_profiles import SavedWebBridgeProfile, WebBridgeProfileStore

        workspace = (root / "workspaces" / side).resolve(strict=True)
        profile_name = PROFILE_NAMES[side]
        session_id = saved_web_bridge_session_id(profile_name)
        access = AccessProfile.WORKSPACE_WRITE
        tools = BASELINE_TOOLS if side == "baseline" else OPTIMIZED_TOOLS
        server_profile = ManagedServerProfile(
            name="relaydesk-benchmark",
            argv=("python", "benchmark_server.py"),
            env={}, env_allowlist=frozenset(), host_hint="127.0.0.1",
            ready_url=f"http://127.0.0.1:{DEV_SERVER_PORT}/health",
        )
        profile = SavedWebBridgeProfile(
            name=profile_name,
            target_profile="chatgpt-web",
            tools=tuple(tools),
            repository=str(workspace),
            verification_commands=VERIFICATION_COMMANDS,
            server_profiles=(server_profile.to_public_dict(),),
            browser_external_https=False,
            browser_allowed_domains=(), browser_denied_domains=(),
            browser_headed=False, browser_user_takeover=False,
            browser_network_inspection=False,
            browser_payment_confirmation=False, browser_allowed_emails=(),
            deadline_seconds=DEADLINE_SECONDS,
            tunnel="cloudflare", public_url=None, language="en",
            access_profile=access, port=port, tunnel_timeout_seconds=30.0,
        )
        WebBridgeProfileStore().put(profile, replace_existing=False)

        sessions = SessionStore(session_dir())
        sessions.create(
            workspace,
            f"Real ChatGPT benchmark {side}",
            access,
            branch="main",
            session_id=session_id,
        )
        credentials = BridgeCredentialStore()
        credential = credentials.set(session_id)
        secret = credential.pop("secret")
        credential_reference = str(credential["reference"])
        credential_fingerprint = str(credential["fingerprint"])

        origin_core = Origin(OriginKind.HOSTED_CLIENT, f"chatgpt-benchmark-{side}-core")
        origin_hosted = Origin(OriginKind.HOSTED_CLIENT, f"chatgpt-benchmark-{side}-hosted")
        core = CoreToolBridge(
            workspace, sessions, session_id, CORE_BASELINE_TOOLS,
            hosted_origin=origin_core,
            audit_path=runtime_dir() / "vnext" / "audit.jsonl",
            verification_commands=VERIFICATION_COMMANDS,
        )
        hosted_tools = HOSTED_BASELINE_TOOLS + (MANAGED_CHECK_TOOLS if side == "optimized" else ())
        browser_policy = BrowserAccessPolicy(
            session_id=session_id,
            localhost=True,
            external_https=False,
            headed=False,
            user_takeover=False,
            network_inspection=False,
            payment_confirmation=False,
        )
        hosted_runtime = HostedToolsRuntime(
            workspace, sessions, session_id, hosted_tools,
            access_profile=access,
            hosted_origin=origin_hosted,
            server_profiles=(server_profile,),
            browser_policy=browser_policy,
            verification_commands=VERIFICATION_COMMANDS,
            audit_path=runtime_dir() / "vnext" / "audit.jsonl",
            artifact_store=ArtifactStore(session_id),
        )
        low_level = CompositeHostedBridge((core, hosted_runtime))
        runtimes: list[Any] = [core, hosted_runtime]
        if side == "optimized":
            autonomy = AutonomyRuntime(
                workspace, sessions, session_id, AUTONOMY_TOOLS,
                access_profile=access,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "chatgpt-benchmark-optimized-autonomy"),
                connection_profile=profile_name,
                verification_commands=VERIFICATION_COMMANDS,
                operation_runtime=low_level,
                client_kind="chatgpt-web",
            )
            runtimes.append(autonomy)
        composite = CompositeHostedBridge(tuple(runtimes))
        descriptors = composite.descriptors()
        expected_names = set(tools)
        descriptor_names = {item.name for item in descriptors}
        if descriptor_names != expected_names:
            raise RuntimeError(
                f"exact tool-set mismatch before serving: expected {sorted(expected_names)}, got {sorted(descriptor_names)}"
            )
        recorder = _CallRecorder(side_root / "events" / "calls.jsonl", secrets.token_bytes(32))
        runtime = _RecordingRuntime(composite, recorder)

        job = _create_child_job()
        tunnel = start_cloudflare_quick_tunnel(port, timeout_seconds=30.0, job=job)
        public_url = tunnel.public_url.rstrip("/")
        public_host = urlsplit(public_url).hostname or ""
        app = build_oauth_proxy_asgi_app(
            runtime,
            lambda: credentials.resolve(credential_reference),
            public_url=public_url,
            deadline_seconds=DEADLINE_SECONDS,
            state_dir=oauth_state_dir(),
        )
        app = _ProtocolMetadataMiddleware(app, side_root / "events" / "protocol.jsonl")
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        )
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        server_thread = threading.Thread(target=server.run, name=f"benchmark-{side}-uvicorn", daemon=True)
        server_thread.start()
        _wait_port(port)

        async def local_probe() -> tuple[list[Any], str]:
            headers = {
                "Authorization": f"Bearer {secret}",
                "Host": public_host,
            }
            url = f"http://127.0.0.1:{port}/mcp"
            async with streamable_http_transport(url, headers=headers, timeout_seconds=20.0) as streams:
                async with ClientSession(
                    streams[0], streams[1],
                    read_timeout_seconds=timedelta(seconds=20),
                ) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    return list(listed.tools), url

        listed_tools, local_url = anyio.run(local_probe)
        local_probe_finished_at = time.time()
        listed_names = {item.name for item in listed_tools}
        expected_wire_names = {wire_tool_name(name) for name in expected_names}
        if listed_names != expected_wire_names:
            missing = sorted(expected_wire_names - listed_names)
            extra = sorted(listed_names - expected_wire_names)
            raise RuntimeError(
                f"local MCP tools/list mismatch: missing={missing}, extra={extra}"
            )

        public_probe: dict[str, Any] = {
            "protected_resource": False,
            "authorization_server": False,
            "route": None,
        }
        deadline = time.monotonic() + 45.0
        public_errors: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            if tunnel.process.poll() is not None:
                raise RuntimeError(
                    f"Cloudflare quick tunnel exited during public OAuth probe "
                    f"with code {tunnel.process.returncode}"
                )

            attempt_errors: dict[str, str] = {}
            for trust_env, route in (
                (True, "environment_proxy"),
                (False, "direct"),
            ):
                try:
                    # Prefer the machine's configured outbound proxy, but fall
                    # back to a direct request. A stale/broken HTTP(S)_PROXY
                    # must not make the harness kill an otherwise healthy public
                    # Cloudflare tunnel before ChatGPT can connect to it.
                    with httpx.Client(
                        timeout=5.0,
                        follow_redirects=False,
                        trust_env=trust_env,
                    ) as client:
                        protected = client.get(
                            f"{public_url}/.well-known/oauth-protected-resource/mcp"
                        )
                        authorization = client.get(
                            f"{public_url}/.well-known/oauth-authorization-server"
                        )

                    public_probe = {
                        "protected_resource": protected.status_code == 200,
                        "authorization_server": authorization.status_code == 200,
                        "protected_resource_status": protected.status_code,
                        "authorization_server_status": authorization.status_code,
                        "route": route,
                    }
                    if (
                        public_probe["protected_resource"]
                        and public_probe["authorization_server"]
                    ):
                        break
                except Exception as exc:
                    attempt_errors[route] = f"{type(exc).__name__}: {exc}"

            if (
                public_probe["protected_resource"]
                and public_probe["authorization_server"]
            ):
                break

            if attempt_errors:
                public_errors.append(attempt_errors)
                public_errors = public_errors[-6:]
            time.sleep(0.5)

        if not public_probe["protected_resource"] or not public_probe["authorization_server"]:
            raise RuntimeError(
                "public OAuth metadata probe failed after proxy+direct retries: "
                f"probe={public_probe}, recent_errors={public_errors}"
            )

        browser_public = {
            "localhost": True,
            "external_https": False,
            "headed": False,
            "user_takeover": False,
            "network_inspection": False,
            "payment_confirmation": False,
            "personal_chrome_visible": False,
        }
        state.update(
            {
                "status": "ready",
                "profile": profile_name,
                "display_name": DISPLAY_NAMES[side],
                "session_id": session_id,
                "pid": os.getpid(),
                "tunnel_pid": tunnel.process.pid,
                "port": port,
                "public_url": public_url,
                "mcp_url": f"{public_url}/mcp",
                "local_mcp_url": local_url,
                "credential_reference": credential_reference,
                "credential_fingerprint": credential_fingerprint,
                "runtime_root": str(runtime_dir()),
                "config_root": str(config_dir()),
                "workspace": str(workspace),
                "access_profile": access.value,
                "deadline_seconds": DEADLINE_SECONDS,
                "tools": list(tools),
                "internal_tool_names_digest": _tool_names_digest(tools),
                "tool_names_digest": _tool_names_digest(item.name for item in listed_tools),
                "tool_schema_digest": _descriptor_digest(listed_tools),
                "verification_commands": [list(item) for item in VERIFICATION_COMMANDS],
                "server_profiles": [server_profile.to_public_dict()],
                "browser_policy": browser_public,
                "local_initialize": True,
                "local_tools_list": True,
                "local_probe_finished_at": local_probe_finished_at,
                "public_oauth_metadata": public_probe,
                "oauth_state_dir": str(oauth_state_dir()),
                "main_repository": str(ROOT),
                "main_repository_used_as_fixture": False,
            }
        )
        _atomic_json(state_path, state)

        control_request = side_root / "control" / "request.json"
        control_response = side_root / "control" / "response.json"
        last_control_id = None
        while server_thread.is_alive():
            request = _json_load(control_request, {})
            if isinstance(request, dict) and request.get("id") and request.get("id") != last_control_id:
                last_control_id = str(request["id"])
                action = request.get("action")
                response: dict[str, Any] = {"id": last_control_id, "action": action, "ok": True, "ts": time.time()}
                try:
                    if action == "cleanup":
                        hosted_runtime.cleanup_session()
                    elif action == "stop":
                        hosted_runtime.cleanup_session()
                        server.should_exit = True
                    elif action == "ping":
                        pass
                    else:
                        response.update({"ok": False, "error": "unknown control action"})
                except Exception as exc:
                    response.update({"ok": False, "error": type(exc).__name__})
                _atomic_json(control_response, response)
            time.sleep(0.25)
        return 0
    except Exception as exc:
        state["status"] = "error"
        state["error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(state_path, state)
        return 1
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                server.should_exit = True
        if hosted_runtime is not None:
            with contextlib.suppress(Exception):
                hosted_runtime.cleanup_session()
        if tunnel is not None:
            with contextlib.suppress(Exception):
                tunnel.process.terminate()


def _protocol_events(root: Path, side: str) -> list[dict[str, Any]]:
    path = root / "side" / side / "events" / "protocol.jsonl"
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _external_status(state_path: Path) -> dict[str, Any]:
    state = _json_load(state_path, {})
    if not isinstance(state, dict) or state.get("status") != "ready":
        raise RuntimeError("benchmark is not prepared")
    root = Path(state["root"])
    result: dict[str, Any] = {"status": "ready", "sides": {}}
    all_connected = True
    for side in ("baseline", "optimized"):
        events = _protocol_events(root, side)
        local_cutoff = float(state["sides"][side].get("local_probe_finished_at", 0.0))
        external_initializes = [
            item for item in events
            if item.get("method") == "initialize" and float(item.get("ts", 0)) > local_cutoff
        ]
        external_lists = [
            item for item in events
            if item.get("method") == "tools/list" and float(item.get("ts", 0)) > local_cutoff
        ]
        # Local probe also produces tools/list, so require a tools/list after the
        # latest external initialize instead of merely any tools/list event.
        latest_init = max((float(item.get("ts", 0)) for item in external_initializes), default=0.0)
        post_init_lists = [item for item in external_lists if float(item.get("ts", 0)) >= latest_init and latest_init > 0]
        connected = bool(external_initializes and post_init_lists)
        all_connected = all_connected and connected
        latest = external_initializes[-1] if external_initializes else {}
        result["sides"][side] = {
            "connected": connected,
            "client_name": latest.get("client_name"),
            "client_version": latest.get("client_version"),
            "initialize_events": len(external_initializes),
            "tools_list_after_initialize": len(post_init_lists),
            "expected_tool_names_digest": state["sides"][side]["tool_names_digest"],
            "expected_tool_schema_digest": state["sides"][side]["tool_schema_digest"],
        }
    result["both_connected"] = all_connected
    return result


def _control(root: Path, side: str, action: str, *, timeout: float = 15.0) -> dict[str, Any]:
    control = root / "side" / side / "control"
    request_id = uuid.uuid4().hex
    _atomic_json(control / "request.json", {"id": request_id, "action": action, "ts": time.time()})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _json_load(control / "response.json", {})
        if isinstance(response, dict) and response.get("id") == request_id:
            return response
        time.sleep(0.1)
    raise RuntimeError(f"{side} bridge did not acknowledge control action {action}")


def _clear_cross_run_task_state(root: Path, side: str, session_id: str) -> None:
    runtime = root / "side" / side / "runtime"
    session = runtime / "vnext" / "sessions" / session_id
    for name in ("task_state.json",):
        with contextlib.suppress(FileNotFoundError):
            (session / name).unlink()
    journal = session / "plan-journals"
    if journal.exists():
        _remove_tree(journal)


def _activate_run(state_path: Path, run_index: int) -> dict[str, Any]:
    state = _json_load(state_path, {})
    if not isinstance(state, dict) or state.get("status") != "ready":
        raise RuntimeError("benchmark is not ready")
    if not 1 <= run_index <= len(state["run_order"]):
        raise ValueError("run index is out of range")
    item = dict(state["run_order"][run_index - 1])
    if int(state.get("next_run_index", 1)) != run_index:
        raise RuntimeError("run activation is out of the pre-recorded order")
    root = Path(state["root"])
    side = str(item["variant"])
    task_id = str(item["task_id"])
    preserve = task_id == "10b"
    _control(root, side, "cleanup")
    if not preserve:
        _clear_cross_run_task_state(root, side, str(state["sides"][side]["session_id"]))
    workspace = _reset_workspace(root, side, task_id, preserve=preserve)
    source_task = "10" if task_id.startswith("10") else task_id
    expected = state["fixtures"][source_task][side]
    if not preserve:
        if workspace["head"] != expected["head"] or workspace["content_digest"] != expected["content_digest"]:
            raise RuntimeError("activated workspace does not match immutable fixture")
        if _sha256_text(workspace["git_status"]) != expected["dirty_status_digest"]:
            raise RuntimeError("activated workspace dirty state does not match immutable fixture")
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from karox.sessions import SessionStore

    session_id = str(state["sides"][side]["session_id"])
    sessions = SessionStore(root / "side" / side / "runtime" / "vnext" / "sessions")
    sessions.validate_repository(sessions.load(session_id), Path(workspace["path"]))
    run_id = f"run-{run_index:02d}-{task_id}-{side}-{uuid.uuid4().hex[:8]}"
    prompt = TASKS[task_id]["prompt"]
    active = {
        "schema_version": 1,
        "run_id": run_id,
        "run_index": run_index,
        "task_id": task_id,
        "variant": side,
        "profile": PROFILE_NAMES[side],
        "prompt_sha256": _sha256_text(prompt),
        "started_at": time.time(),
        "starting_revision": workspace["head"],
        "starting_content_digest": workspace["content_digest"],
        "starting_git_status_sha256": _sha256_text(workspace["git_status"]),
    }
    _atomic_json(root / "side" / side / "control" / "active-run.json", active)
    _atomic_json(
        SCRATCH_NEXT,
        {
            "run_id": run_id,
            "run_index": run_index,
            "task_id": task_id,
            "variant": side,
            "profile": PROFILE_NAMES[side],
            "display_name": DISPLAY_NAMES[side],
            "prompt": prompt,
            "prompt_sha256": active["prompt_sha256"],
            "fixture_head": workspace["head"],
            "fixture_content_digest": workspace["content_digest"],
        },
    )
    state["active_run"] = active
    state["benchmark_started"] = True
    _atomic_json(state_path, state)
    return active


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def _repair_invalid_baseline_run(state_path: Path) -> dict[str, Any]:
    """Archive an invalid baseline run and repair only its disposable session binding."""
    state = _json_load(state_path, {})
    if not isinstance(state, dict) or state.get("status") != "ready":
        raise RuntimeError("benchmark is not ready")
    active = state.get("active_run")
    if not isinstance(active, dict):
        raise RuntimeError("no active run exists to invalidate")
    if active.get("run_index") != 1 or active.get("variant") != "baseline":
        raise RuntimeError("repair is restricted to invalid baseline run #1")

    root = Path(state["root"])
    side = "baseline"
    workspace = (root / "workspaces" / side).resolve(strict=True)
    side_root = root / "side" / side
    session_id = str(state["sides"][side]["session_id"])
    run_id = str(active["run_id"])
    finished_at = time.time()

    # Capture the invalid run before changing any session metadata.  The recorder
    # stores no raw arguments or repository content, only tool names/byte counts,
    # salted fingerprints, timing, and exception classes.
    calls = [
        item for item in _load_jsonl(side_root / "events" / "calls.jsonl")
        if item.get("run_id") == run_id
    ]

    os.environ.update(_side_env(root, side))
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from karox.bridge import BridgeCredentialStore
    from karox.mcp_client import streamable_http_transport
    from karox.models import repository_fingerprint
    from karox.proxy_server import wire_tool_name
    from karox.sessions import SessionStore
    from karox.tool_telemetry import ToolTraceStore

    sessions = SessionStore(side_root / "runtime" / "vnext" / "sessions")
    record = sessions.load(session_id)
    recorded_fingerprint = record.repo_fingerprint
    current_fingerprint = repository_fingerprint(workspace)
    if recorded_fingerprint == current_fingerprint:
        raise RuntimeError("baseline session fingerprint is not mismatched; refusing unrelated repair")

    trace_store = ToolTraceStore(
        session_id,
        root=side_root / "runtime" / "vnext" / "tool-telemetry",
    )
    trace_rows = [
        row for row in trace_store.list(limit=10_000)
        if float(active["started_at"]) <= _iso_to_timestamp(str(row.get("started_at", ""))) <= finished_at
    ]
    invalid_evidence = {
        "schema_version": 1,
        "classification": "INVALID",
        "benchmark_run_id": run_id,
        "run_index": 1,
        "task_id": "01",
        "variant": "baseline",
        "reason": "benchmark_harness_replaced_git_marker_after_session_binding",
        "wire_error": "denied",
        "root_cause": "SessionStore.validate_repository rejected a changed repository fingerprint after activation replaced the disposable workspace .git marker",
        "recorded_repository_fingerprint": recorded_fingerprint,
        "current_repository_fingerprint": current_fingerprint,
        "repository_fingerprint_mismatch": True,
        "mcp_tool_calls": len(calls),
        "calls": [
            {
                "tool": str(item.get("tool", "")),
                "success": bool(item.get("success")),
                "error_code": item.get("error_code"),
                "input_size_bytes": int(item.get("input_size_bytes", 0)),
                "duration_ms": float(item.get("duration_ms", 0.0)),
            }
            for item in calls
        ],
        "product_trace_rows": len(trace_rows),
        "product_trace_error_codes": sorted({
            str(row.get("error_code")) for row in trace_rows if row.get("error_code")
        }),
        "counted_as_failed_result": False,
        "next_run_index_after_invalidation": 1,
        "optimized_run_started": False,
        "captured_at": finished_at,
    }
    evidence_path = ROOT / "scratch" / "gpt_web_real_benchmark_invalid_run_01.json"
    _atomic_json(evidence_path, invalid_evidence)

    # Stop attributing subsequent repair/probe calls to the invalid measurement.
    _atomic_json(side_root / "control" / "active-run.json", {})
    state.setdefault("invalid_runs", []).append(
        {
            "run_index": 1,
            "run_id": run_id,
            "classification": "INVALID",
            "evidence": str(evidence_path),
        }
    )
    state.pop("active_run", None)
    state["next_run_index"] = 1
    state["benchmark_started"] = False
    _atomic_json(state_path, state)

    # Rebind only the isolated Baseline benchmark session to the disposable
    # workspace identity that exists now. No live profile or optimized session is
    # read or mutated here.
    with sessions.mutate(session_id, "real-chatgpt-baseline-invalid-repair", ttl_seconds=30.0) as mutable:
        mutable.repo_fingerprint = current_fingerprint
    rebound = sessions.load(session_id)
    sessions.validate_repository(rebound, workspace)

    # Recreate the exact task-01 snapshot without replacing .git, then prove the
    # session binding remains valid across that reset.
    marker_before = (workspace / ".git").stat()
    workspace_state = _reset_workspace(root, side, "01")
    marker_after = (workspace / ".git").stat()
    if (marker_before.st_dev, marker_before.st_ino, marker_before.st_mode) != (
        marker_after.st_dev, marker_after.st_ino, marker_after.st_mode
    ):
        raise RuntimeError("baseline reset changed repository identity after repair")
    rebound = sessions.load(session_id)
    sessions.validate_repository(rebound, workspace)
    expected = state["fixtures"]["01"][side]
    if workspace_state["head"] != expected["head"]:
        raise RuntimeError("repaired baseline HEAD does not match task-01 fixture")
    if workspace_state["content_digest"] != expected["content_digest"]:
        raise RuntimeError("repaired baseline content does not match task-01 fixture")
    if _sha256_text(workspace_state["git_status"]) != expected["dirty_status_digest"]:
        raise RuntimeError("repaired baseline dirty state does not match task-01 fixture")

    # Exercise the same four read-only tools that the invalid ChatGPT run could
    # not use. This happens before a replacement active-run file is created, so
    # the probe is not benchmark telemetry.
    import anyio
    from datetime import timedelta
    from mcp import ClientSession

    credential_ref = str(state["sides"][side]["credential_reference"])
    secret = BridgeCredentialStore().resolve(credential_ref)
    public_url = str(state["sides"][side]["public_url"])
    public_host = urlsplit(public_url).hostname or ""
    local_url = str(state["sides"][side]["local_mcp_url"])
    probes = (
        ("karox.repo.search", {"query": "normalize_connector_name", "max_results": 5}),
        ("karox.repo.list_files", {"pattern": "src/relaydesk/*.py"}),
        ("karox.git.status", {}),
        ("karox.runtime.status", {}),
    )

    async def run_probe() -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {secret}", "Host": public_host}
        outcomes: list[dict[str, Any]] = []
        async with streamable_http_transport(local_url, headers=headers, timeout_seconds=20.0) as streams:
            async with ClientSession(
                streams[0], streams[1],
                read_timeout_seconds=timedelta(seconds=20),
            ) as client:
                await client.initialize()
                for internal_name, arguments in probes:
                    result = await client.call_tool(wire_tool_name(internal_name), arguments)
                    structured = getattr(result, "structuredContent", None)
                    explicit_ok = structured.get("ok") if isinstance(structured, Mapping) else None
                    passed = not bool(getattr(result, "isError", False)) and explicit_ok is not False
                    outcomes.append({"tool": internal_name, "passed": passed})
                    if not passed:
                        raise RuntimeError(f"post-repair MCP probe failed: {internal_name}")
        return outcomes

    probe_outcomes = anyio.run(run_probe)
    repair = {
        "status": "repaired",
        "classification": "INVALID",
        "invalid_evidence": str(evidence_path),
        "baseline_session_id": session_id,
        "baseline_session_revision": rebound.revision,
        "repository_fingerprint": rebound.repo_fingerprint,
        "fixture_head": workspace_state["head"],
        "fixture_content_digest": workspace_state["content_digest"],
        "fixture_git_status_sha256": _sha256_text(workspace_state["git_status"]),
        "repository_identity_preserved": True,
        "read_only_probe": probe_outcomes,
        "next_run_index": 1,
        "optimized_run_started": False,
    }
    state = _json_load(state_path, {})
    state["baseline_invalid_repair"] = repair
    _atomic_json(state_path, state)
    return repair


def _evaluate_task(workspace: Path, task_id: str) -> tuple[bool, list[str]]:
    problems: list[str] = []
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace / "src")

    def check_json(rel: str) -> dict[str, Any]:
        value = _json_load(workspace / rel, {})
        if not isinstance(value, dict):
            problems.append(f"{rel} is missing or invalid")
            return {}
        return value

    def run_pytest(*targets: str) -> bool:
        completed = subprocess.run(
            (sys.executable, "-m", "pytest", *targets), cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=90, check=False,
        )
        if completed.returncode != 0:
            problems.append(f"verification failed: {' '.join(targets) or 'pytest'}")
            return False
        return True

    if task_id == "01":
        value = check_json("benchmark/answer.json")
        if value.get("implementation_file") != "src/relaydesk/validation.py":
            problems.append("wrong implementation_file")
        if value.get("symbol") != "normalize_connector_name":
            problems.append("wrong symbol")
        tests = value.get("test_files")
        if not isinstance(tests, list) or "tests/test_validation.py" not in tests:
            problems.append("related validation test not identified")
    elif task_id == "02":
        value = check_json("benchmark/answer.json")
        flow = value.get("flow")
        expected = [
            "ConnectScreen.submit", "ConnectionService.connect",
            "RuntimeSupervisor.launch", "WorkerRuntime.start",
        ]
        normalized = [str(item).split(".")[-2] + "." + str(item).split(".")[-1] if str(item).count(".") >= 1 else str(item) for item in flow] if isinstance(flow, list) else []
        if normalized != expected:
            problems.append("call flow is incorrect")
    elif task_id == "03":
        value = check_json("benchmark/diagnosis.json")
        if "test_lease_expires_at_boundary" not in str(value.get("failing_test", "")):
            problems.append("wrong failing test")
        if "lease_expired" not in str(value.get("root_cause_symbol", "")):
            problems.append("wrong root cause symbol")
        code = str(value.get("root_cause_code", "")).lower()
        if not any(marker in code for marker in ("strict", ">", "boundary")):
            problems.append("diagnosis does not identify strict boundary comparison")
        status = _git_status(workspace)
        changed = [line for line in status.splitlines() if line and not line[3:].startswith("benchmark/")]
        if changed:
            problems.append("diagnosis task modified production/tests")
    elif task_id == "04":
        run_pytest("tests/test_validation.py")
        code = (
            "from relaydesk.validation import normalize_connector_name\n"
            "for x in ['a/b', 'a\\\\b']:\n"
            "    try: normalize_connector_name(x)\n"
            "    except ValueError: pass\n"
            "    else: raise SystemExit(2)\n"
        )
        completed = subprocess.run(
            (sys.executable, "-c", code), cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        if completed.returncode != 0:
            problems.append("slash/backslash validation is incomplete")
    elif task_id == "05":
        run_pytest("tests/test_service.py", "tests/test_runtime.py", "tests/test_tui.py")
        code = (
            "from relaydesk.models import ConnectionRequest\n"
            "from relaydesk.runtime import RuntimeSupervisor, WorkerRuntime\n"
            "r=ConnectionRequest('demo', timeout_seconds=42)\n"
            "x=RuntimeSupervisor(WorkerRuntime()).launch(r)\n"
            "assert x['timeout_seconds']==42\n"
            "assert RuntimeSupervisor(WorkerRuntime()).launch(ConnectionRequest('demo'))['timeout_seconds']==15\n"
        )
        completed = subprocess.run((sys.executable, "-c", code), cwd=workspace, env=env, timeout=20, check=False)
        if completed.returncode != 0:
            problems.append("timeout propagation/default behavior is incorrect")
    elif task_id == "06":
        run_pytest("tests/test_refactor.py", "tests/test_service.py", "tests/test_runtime.py")
        definitions = 0
        for path in (workspace / "src" / "relaydesk").glob("*.py"):
            definitions += path.read_text(encoding="utf-8").count("def format_status(")
        if definitions != 1:
            problems.append("status formatter still has duplicate production definitions")
    elif task_id == "07":
        value = check_json("benchmark/verification.json")
        if not isinstance(value.get("commands"), list) or not value.get("commands"):
            problems.append("verification commands were not recorded")
        run_pytest("tests/test_validation.py")
        status = _git_status(workspace)
        allowed = {"src/relaydesk/validation.py", "tests/test_validation.py", "benchmark/verification.json"}
        for line in status.splitlines():
            path = line[3:].strip().replace("\\", "/")
            if path and path not in allowed and not path.startswith("benchmark/"):
                problems.append(f"unexpected file change: {path}")
    elif task_id == "08":
        value = check_json("benchmark/long_verification.json")
        marker = workspace / "benchmark" / "long_verify.done"
        if not marker.is_file() or "LONG_VERIFY_COMPLETED" not in marker.read_text(encoding="utf-8"):
            problems.append("long verification completion marker is missing")
        if value.get("success") is not True:
            problems.append("long verification report does not record success")
    elif task_id == "09":
        value = check_json("benchmark/ui_observation.json")
        expected = {
            "title": "RelayDesk Benchmark",
            "heading": "Connector Console",
            "status_text": "ready",
            "connect_button_present": True,
        }
        for key, expected_value in expected.items():
            if value.get(key) != expected_value:
                problems.append(f"UI observation mismatch: {key}")
    elif task_id == "10a":
        # Optimized recovery may use KaroX task state while baseline can persist
        # an explicit repository note. Either is accepted; the mechanism is a
        # measured outcome and is never dictated in the prompt.
        recovery_file = workspace / "benchmark" / "recovery.json"
        task_state = (
            Path(os.environ.get("KAROX_VNEXT_RUNTIME_DIR", ""))
            / "vnext" / "sessions"
        )
        has_task_state = any(task_state.glob("*/task_state.json")) if task_state.is_dir() else False
        if not recovery_file.exists() and not has_task_state:
            problems.append("no durable recovery state was created")
    elif task_id == "10b":
        run_pytest("tests/test_reconnect.py")
        code = "from relaydesk.reconnect import reconnect_delay\nassert reconnect_delay(10) == 30\n"
        completed = subprocess.run((sys.executable, "-c", code), cwd=workspace, env=env, timeout=20, check=False)
        if completed.returncode != 0:
            problems.append("reconnect delay is not capped at 30 seconds")
    else:
        problems.append(f"unknown task id: {task_id}")
    return not problems, problems


def _evaluate_run(state_path: Path) -> dict[str, Any]:
    state = _json_load(state_path, {})
    if not isinstance(state, dict) or not isinstance(state.get("active_run"), dict):
        raise RuntimeError("no active benchmark run")
    active = dict(state["active_run"])
    root = Path(state["root"])
    side = str(active["variant"])
    task_id = str(active["task_id"])
    workspace = root / "workspaces" / side
    finished_at = time.time()
    success, problems = _evaluate_task(workspace, task_id)

    calls = [
        item for item in _load_jsonl(root / "side" / side / "events" / "calls.jsonl")
        if item.get("run_id") == active["run_id"]
    ]
    fingerprints: dict[str, int] = {}
    resources: dict[str, int] = {}
    for item in calls:
        fp = str(item.get("input_fingerprint") or "")
        if fp:
            fingerprints[fp] = fingerprints.get(fp, 0) + 1
        resource = str(item.get("resource_fingerprint") or "")
        if resource:
            resources[resource] = resources.get(resource, 0) + 1

    # Product telemetry stores only metadata and byte counts.  Read its rows by
    # session through the public store with an explicit root, never raw args.
    os.environ.update(_side_env(root, side))
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from karox.tool_telemetry import ToolTraceStore

    trace_store = ToolTraceStore(
        str(state["sides"][side]["session_id"]),
        root=root / "side" / side / "runtime" / "vnext" / "tool-telemetry",
    )
    trace_rows = [
        row for row in trace_store.list(limit=10_000)
        if active["started_at"] <= _iso_to_timestamp(str(row.get("started_at", ""))) <= finished_at
    ]
    input_bytes = sum(int(row.get("input_size_bytes", 0)) for row in trace_rows)
    inline_bytes = sum(int(row.get("inline_output_bytes", 0)) for row in trace_rows)
    artifact_bytes = sum(int(row.get("artifact_output_bytes", 0)) for row in trace_rows)
    validation_failures = sum(row.get("error_code") == "invalid_request" for row in trace_rows)
    permission_failures = sum(row.get("error_code") == "denied" for row in trace_rows)
    idempotent_replays = sum(bool(row.get("idempotent_replay")) for row in trace_rows)
    managed_jobs_started = sum(item.get("tool") == "karox.checks.start" for item in calls)
    unsafe_attempts = sum(
        int(row.get("permission_tier", 0)) >= 3 or bool(row.get("user_gate_required"))
        for row in trace_rows
    )

    result = {
        "schema_version": 1,
        "benchmark_run_id": active["run_id"],
        "run_index": active["run_index"],
        "task_id": task_id,
        "variant": side,
        "profile": active["profile"],
        "repository_digest": active["starting_content_digest"],
        "starting_revision": active["starting_revision"],
        "ending_revision": _git_head(workspace),
        "ending_content_digest": _tree_digest(workspace),
        "task_success": success,
        "correctness_gate": {"passed": success, "problems": problems},
        "mcp_tool_calls": len(calls),
        "unique_tools_used": sorted({str(item.get("tool")) for item in calls}),
        "repeated_identical_calls": sum(max(0, count - 1) for count in fingerprints.values()),
        "repeated_file_reads": sum(max(0, count - 1) for count in resources.values()),
        "total_tool_input_bytes": input_bytes,
        "total_inline_output_bytes": inline_bytes,
        "artifact_output_bytes": artifact_bytes,
        "total_tool_result_bytes": inline_bytes + artifact_bytes,
        "wall_clock_duration_seconds": round(finished_at - float(active["started_at"]), 3),
        "time_to_first_relevant_action_seconds": (
            None if not calls else round(float(calls[0]["ts"]) - float(active["started_at"]), 3)
        ),
        "retries": idempotent_replays,
        "validation_schema_failures": validation_failures,
        "permission_failures": permission_failures,
        "idempotent_replays": idempotent_replays,
        "user_interventions": 0,
        "manual_confirmations": 0,
        "managed_jobs_started": managed_jobs_started,
        "context_recovery_success": success if task_id == "10b" else None,
        "unsafe_attempt_count": unsafe_attempts,
        "final_checks": problems if problems else ["deterministic evaluator passed"],
        "finished_at": finished_at,
    }
    results_path = root / "results" / f"run-{int(active['run_index']):02d}.json"
    _atomic_json(results_path, result)
    _control(root, side, "cleanup")
    state.setdefault("results", []).append({"run_index": active["run_index"], "path": str(results_path), "passed": success})
    state["next_run_index"] = int(active["run_index"]) + 1
    state.pop("active_run", None)
    _atomic_json(state_path, state)
    return result


def _iso_to_timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _status(state_path: Path) -> dict[str, Any]:
    state = _json_load(state_path, {})
    if not isinstance(state, dict):
        raise RuntimeError("benchmark state is unreadable")
    if state.get("status") != "ready":
        return state
    root = Path(state["root"])
    sides: dict[str, Any] = {}
    for side in ("baseline", "optimized"):
        response = _control(root, side, "ping", timeout=5.0)
        side_state = _json_load(Path(state["sides"][side]["state_path"]), {})
        sides[side] = {
            "alive": bool(response.get("ok")),
            "profile": side_state.get("profile"),
            "session_id": side_state.get("session_id"),
            "port": side_state.get("port"),
            "mcp_url": side_state.get("mcp_url"),
            "credential_reference": side_state.get("credential_reference"),
            "tool_names_digest": side_state.get("tool_names_digest"),
            "tool_schema_digest": side_state.get("tool_schema_digest"),
            "public_oauth_metadata": side_state.get("public_oauth_metadata"),
        }
    return {"status": "ready", "benchmark_id": state.get("benchmark_id"), "sides": sides}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--state", type=Path, default=SCRATCH_STATE)

    serve = sub.add_parser("serve-side")
    serve.add_argument("--root", type=Path, required=True)
    serve.add_argument("--side", choices=("baseline", "optimized"), required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--state", type=Path, required=True)

    status = sub.add_parser("status")
    status.add_argument("--state", type=Path, default=SCRATCH_STATE)

    external = sub.add_parser("external-status")
    external.add_argument("--state", type=Path, default=SCRATCH_STATE)

    activate = sub.add_parser("activate")
    activate.add_argument("--state", type=Path, default=SCRATCH_STATE)
    activate.add_argument("--run-index", type=int, required=True)

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--state", type=Path, default=SCRATCH_STATE)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "prepare":
        return _prepare(args.state.resolve())
    if args.command == "serve-side":
        return _serve_side(args.root.resolve(), args.side, args.port, args.state.resolve())
    if args.command == "status":
        print(json.dumps(_status(args.state.resolve()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "external-status":
        print(json.dumps(_external_status(args.state.resolve()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "activate":
        print(json.dumps(_activate_run(args.state.resolve(), args.run_index), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "evaluate":
        print(json.dumps(_evaluate_run(args.state.resolve()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
