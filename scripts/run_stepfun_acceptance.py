"""Opt-in Step 5 Preview acceptance through the production agent and Core.

Only disposable fixture repositories are sent to the provider. Both model
workers are read-only: model-produced code and commands are never executed.
The supplied
credential is read into memory, never installed into a profile or written to
evidence. This live command is deliberately excluded from automated tests.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from karox import __version__  # noqa: E402
from karox.agent import AgentKernel, AgentLimits  # noqa: E402
from karox.core import CoreRuntime  # noqa: E402
from karox.models import AccessProfile, Capability, Origin, OriginKind  # noqa: E402
from karox.policy import CapabilityPolicy  # noqa: E402
from karox.providers import OpenAIChatCompletionsProvider  # noqa: E402
from karox.sessions import SessionStore  # noqa: E402


def validate_answer(lane: str, content: str | None) -> bool:
    try:
        answer = json.loads(content or "")
    except (ValueError, TypeError):
        return False
    if not isinstance(answer, dict) or answer.get("operation") != "subtraction":
        return False
    if lane == "review":
        return (type(answer.get("observed")) is int and answer["observed"] == -1
                and type(answer.get("expected")) is int and answer["expected"] == 5)
    cases = answer.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        return False
    found = set()
    for case in cases:
        if not isinstance(case, dict) or any(type(case.get(k)) is not int
                                           for k in ("a", "b", "expected", "observed")):
            return False
        a, b = case["a"], case["b"]
        if case["expected"] != a + b or case["observed"] != a - b:
            return False
        found.add((a, b))
    return found == {(2, 3), (-3, 1), (0, 0)}


def source_identity() -> dict:
    paths = sorted((ROOT / "src/karox").rglob("*.py"))
    paths.extend((Path(__file__).resolve(), ROOT / "pyproject.toml"))
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in paths}
    tree = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {"tree_sha256": tree, "files": hashes, "python": sys.version.split()[0]}


def acceptance_lane(root: Path, key: str, endpoint: str, lane: str) -> dict:
    started = time.monotonic()
    try:
        return _acceptance_lane(root, key, endpoint, lane)
    except Exception as error:
        return {"lane": lane, "status": "failed", "error_type": type(error).__name__,
                "duration_seconds": round(time.monotonic() - started, 2)}


def _acceptance_lane(root: Path, key: str, endpoint: str, lane: str) -> dict:
    started = time.monotonic()
    repository = root / lane / "repository"
    repository.mkdir(parents=True)
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "--quiet"], cwd=repository, env=environment,
                   check=True, capture_output=True, timeout=30)
    fixture = repository / "sample.py"
    fixture.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repository / "verify.py").write_text(
        "from sample import add\nassert add(2, 3) == 5\nprint('fixture check passed')\n",
        encoding="utf-8",
    )
    fixture_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in repository.iterdir() if p.is_file()}
    profile = AccessProfile.READ_ONLY
    task = (
        "Read sample.py and verify.py using repository tools. Design regression cases "
        "for add with (a,b) values (2,3), (-3,1), (0,0). Do not write or execute code. "
        "Return ONLY JSON: operation names the implemented arithmetic in English; "
        "cases is an array of objects with integer a,b,expected,observed fields. "
        "expected is the intended addition result, observed is the current result."
        if lane == "test-design" else
        "Read sample.py using a repository tool. Independently identify the arithmetic "
        "defect. This is a review: do not change files. Return ONLY JSON with "
        "operation (the implemented arithmetic in English), observed (current "
        "integer result of add(2,3)) and expected (intended integer result)."
    )
    sessions = SessionStore(root / lane / "sessions")
    sessions.create(repository, task, profile, session_id=lane)
    origin = Origin(OriginKind.NATIVE_AGENT, "stepfun-live-acceptance")
    policy = CapabilityPolicy(profile)
    grants = {Capability.REPO_READ, Capability.GIT_READ}
    policy.set_grants(origin, grants)
    # AgentKernel requires a verification command even for a read-only answer.
    # Neither CHECKS_RUN nor PROCESS_RUN is granted here, so the model cannot
    # invoke it. It is a fixed no-op, never provider-supplied code.
    core = CoreRuntime(repository, policy, sessions, root / lane / "audit.jsonl",
                       verification_commands=[[sys.executable, "-c", "pass"]])
    provider = OpenAIChatCompletionsProvider(
        endpoint, credential=lambda: key, timeout_seconds=90, max_transport_retries=0,
    )
    events: dict[str, int] = {}
    def observe(event):
        name = event.kind.value
        events[name] = events.get(name, 0) + 1
    kernel = AgentKernel(
        provider=provider, model="step-5-preview", core=core, sessions=sessions,
        origin=origin, limits=AgentLimits(max_steps=10, max_seconds=360),
        max_output_tokens=4096, on_event=observe,
    )
    try:
        report = kernel.run(lane).to_dict()
        fixture_ok = fixture_hashes == {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in repository.iterdir() if p.is_file()
        }
        answer_ok = validate_answer(lane, report.get("provider_message"))
        read_paths = {item.get("path") for item in report.get("answer_basis", [])
                      if item.get("tool") in {"repo.read_file", "repo.read_lines"}}
        required_paths = {"sample.py", "verify.py"} if lane == "test-design" else {"sample.py"}
        read_ok = required_paths.issubset(read_paths)
        return {"lane": lane, "status": "passed" if fixture_ok and answer_ok and read_ok and
                report.get("verified") is True else "failed",
                "fixture_ok": fixture_ok, "answer_ok": answer_ok, "read_ok": read_ok,
                "report": report, "events": events,
                "duration_seconds": round(time.monotonic() - started, 2)}
    except Exception as error:
        return {"lane": lane, "status": "failed", "error_type": type(error).__name__,
                "events": events, "duration_seconds": round(time.monotonic() - started, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", required=True, choices=(
        "https://api.stepfun.ai/v1", "https://api.stepfun.com/v1",
        "https://api.stepfun.ai/step_plan/v1", "https://api.stepfun.com/step_plan/v1",
    ), help="Choose the account region and billing surface explicitly; no paid-route fallback.")
    args = parser.parse_args()
    if args.output.resolve() == args.credential_file.resolve():
        parser.error("evidence output must differ from credential input")
    key = args.credential_file.read_text(encoding="utf-8-sig").strip()
    if not key or any(char.isspace() for char in key):
        parser.error("credential file must contain one nonempty credential")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                            capture_output=True, text=True).stdout.strip()
    before = source_identity()
    with tempfile.TemporaryDirectory(prefix="karox-stepfun-acceptance-") as temporary:
        root = Path(temporary)
        overrides = {"KAROX_VNEXT_CONFIG_DIR": str(root / "config"),
                     "KAROX_VNEXT_RUNTIME_DIR": str(root / "runtime"),
                     "KAROX_LEGACY_CONFIG_DIR": str(root / "legacy")}
        previous = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(acceptance_lane, root, key, args.endpoint, lane)
                           for lane in ("test-design", "review")]
                results = [future.result() for future in futures]
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    after = source_identity()
    evidence = {"model": "step-5-preview", "endpoint": args.endpoint,
                "verified_at_utc": datetime.now(timezone.utc).isoformat(),
                "karox_version": __version__, "base_commit": commit,
                "source_identity": before, "source_unchanged": before == after,
                "adapter_sha256": hashlib.sha256((ROOT / "src/karox/providers.py").read_bytes()).hexdigest(),
                "results": results, "ok": before == after and
                all(item["status"] == "passed" for item in results)}
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2)
    # Defence in depth: the opaque accessor must never become result content.
    serialized = serialized.replace(key, "[REDACTED]")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized + "\n", encoding="utf-8")
    print(json.dumps({"ok": evidence["ok"], "model": "step-5-preview",
                      "lanes": [{"lane": r["lane"], "status": r["status"]} for r in results]}))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
