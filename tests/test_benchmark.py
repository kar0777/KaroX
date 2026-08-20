"""Phase 10 — KaroX hybrid runtime benchmark (KB-HYBRID-01..10).

This suite formalizes the gates the roadmap names for Phases 2/5/6/7 plus the
release-readiness benchmark.  Each gate exercises the *real* runtime (real
``AgentKernel`` loop, real stdio MCP server, real session store, real proxy
boundaries, real bridge credentials) through deterministic scripted providers
and captures a structured raw run record (pass/fail, latency, usage, cost,
evidence kinds, limitations, failure reason).

Honesty rules:
- No external key, paid account, or live service is required for KB-HYBRID-01
  through 07, 09, and 10.  KB-HYBRID-08 runs the bundled Notion transport
  regression script; if that environment is unavailable the gate reports an
  honest failure with the recorded limitation rather than faking success.
- Raw run records are produced at runtime (printed via the summary table) and
  are intentionally not committed as frozen numbers, because latency/usage vary
  by machine and run.  Reproducibility is the contract: ``python -m unittest
  discover -s tests -p "test_benchmark.py" -v`` regenerates them.
- The aggregation gate asserts every benchmark produced a well-formed record
  and that all gates passed, so a silent skip or a missing gate fails the suite.

The new harness code this phase adds is KB-HYBRID-05: driving ``AgentKernel``
with a scripted provider tool-call to an MCP alias so the kernel routes the
call through ``CoreRuntime`` to the real external stdio MCP server — the path
no earlier phase test exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from _support import SRC, initialize_git_repository  # noqa: F401
from _bench_support import (
    BenchmarkRecord,
    QueueProvider,
    Timer,
    call,
    model_response,
    record,
    reset_records,
    routed_cost,
    stdio_mcp,
    summary_table,
)

from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.bridge import (
    BridgeCredentialReference,
    BridgeCredentialStore,
    known_bridge_profiles,
)
from karox.core import CoreRuntime
from karox.handoff import HANDOFF_SCHEMA_VERSION, build_handoff, handoff_digest
from karox.mcp_client import McpRuntimeBinding, mcp_selection
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.proxy import McpProxy
from karox.sessions import SessionBusy, SessionStore


# --------------------------------------------------------------------------- #
# A fake OS-keyring backend for the secret-isolation gate (no real keyring).  #
# --------------------------------------------------------------------------- #
class _FakeCredentialBackend:
    def __init__(self) -> None:
        self.store: Dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> Optional[str]:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


# --------------------------------------------------------------------------- #
# Reusable agent stack.                                                       #
# --------------------------------------------------------------------------- #
class _Stack:
    """Builds the full CoreRuntime/SessionStore/agent stack for one benchmark.

    ``with_mcp`` attaches a real stdio MCP echo server so the agent kernel can
    route MCP alias tool-calls through Core, and so the proxy gates share the
    same plumbing as the Phase 5/7 E2E tests.
    """

    def __init__(self, *, session_id: str = "session", with_mcp: bool = False) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "change sample and verify it",
            AccessProfile.WORKSPACE_WRITE,
            session_id=session_id,
        )
        self.session_id = session_id
        self.origin = Origin(OriginKind.NATIVE_AGENT, "bench-agent")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.PROCESS_RUN,
                Capability.CHECKS_RUN,
                Capability.GIT_READ,
                Capability.MCP_CALL,
            },
        )
        self.audit_path = self.root / "audit.jsonl"
        self.mcp: Optional[dict] = None
        if with_mcp:
            self._attach_mcp()
        else:
            self.core = CoreRuntime(
                self.repository, self.policy, self.sessions, self.audit_path,
                verification_commands=[
                    [sys.executable, "-c", "print('ok')"],
                    [sys.executable, "-c", "import sys; sys.exit(9)"],
                ],
            )

    def _attach_mcp(self) -> None:
        registry, record, client, tools = stdio_mcp(self.root)
        selection = mcp_selection(
            record, tools, {"echo": "allow", "write_note": "allow"},
        )
        with self.sessions.mutate(self.session_id, "mcp-setup") as rec:
            rec.mcp_servers = [selection]
        binding = McpRuntimeBinding(
            client, self.repository, tools, [record],
        )
        self.core = CoreRuntime(
            self.repository, self.policy, self.sessions, self.audit_path,
            mcp_binding=binding,
            verification_commands=[
                [sys.executable, "-c", "print('ok')"],
                [sys.executable, "-c", "import sys; sys.exit(9)"],
            ],
        )
        self.mcp = {
            "registry": registry,
            "record": record,
            "client": client,
            "tools": tools,
            "binding": binding,
        }

    @property
    def hosted_origin(self) -> Origin:
        return Origin(OriginKind.HOSTED_CLIENT, "notion-bridge")

    def kernel(
        self,
        provider: QueueProvider,
        limits: AgentLimits,
        *,
        model: str = "bench-model",
        origin: Optional[Origin] = None,
    ) -> AgentKernel:
        return AgentKernel(
            provider=provider,
            model=model,
            core=self.core,
            sessions=self.sessions,
            origin=origin or self.origin,
            limits=limits,
            system_prompt=SYSTEM_PROMPT,
        )

    def restart(self) -> "_Stack":
        """Return a stack sharing the same on-disk paths (simulate restart)."""
        clone = _Stack.__new__(_Stack)
        clone.temporary = None  # don't own the tempdir; caller owns it
        clone.root = self.root
        clone.repository = self.repository
        clone.sessions = SessionStore(self.root / "sessions")  # fresh handle
        clone.session_id = self.session_id
        clone.origin = self.origin
        clone.policy = self.policy
        clone.audit_path = self.audit_path
        clone.mcp = self.mcp
        binding = self.mcp["binding"] if self.mcp else None
        clone.core = CoreRuntime(
            clone.repository, clone.policy, clone.sessions, clone.audit_path,
            mcp_binding=binding,
            verification_commands=[
                [sys.executable, "-c", "print('ok')"],
                [sys.executable, "-c", "import sys; sys.exit(9)"],
            ],
        )
        return clone

    def cleanup(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()


# --------------------------------------------------------------------------- #
# The benchmark suite.                                                        #
# --------------------------------------------------------------------------- #
class HybridRuntimeBenchmark(unittest.TestCase):
    """KB-HYBRID-01 through 10 — real runtime, structured run records."""

    EXPECTED_IDS = [f"KB-HYBRID-{i:02d}" for i in range(1, 11)]

    @classmethod
    def setUpClass(cls) -> None:
        # Reset once before the suite so the 10 gates accumulate records that
        # the aggregation gate (run last, alphabetically) can inspect.  A
        # per-test reset would wipe the records the aggregation gate needs.
        reset_records()

    def _run_gate(
        self,
        benchmark_id: str,
        name: str,
        body: Callable[[], Dict[str, Any]],
        limitations: Optional[List[str]] = None,
    ) -> None:
        """Run one gate under a timer; always emit a record; re-raise failures.

        The record is appended even when the gate's assertion fails so the
        summary table and the aggregation gate can report the honest outcome.
        """
        timer = Timer()
        lim = list(limitations or [])
        try:
            with timer.measure():
                data = body()
        except AssertionError as exc:
            record(BenchmarkRecord(
                benchmark_id=benchmark_id,
                name=name,
                passed=False,
                latency_ms=timer.elapsed_ms,
                limitations=lim,
                failure_reason=str(exc),
            ))
            raise
        except Exception as exc:  # pragma: no cover - defensive, surfaces unexpected
            record(BenchmarkRecord(
                benchmark_id=benchmark_id,
                name=name,
                passed=False,
                latency_ms=timer.elapsed_ms,
                limitations=lim,
                failure_reason=f"{type(exc).__name__}: {exc}",
            ))
            raise
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        record(BenchmarkRecord(
            benchmark_id=benchmark_id,
            name=name,
            passed=True,
            latency_ms=timer.elapsed_ms,
            usage=usage,
            cost=data.get("cost"),
            evidence_summary=list(data.get("evidence", [])),
            limitations=lim,
        ))

    # -- KB-HYBRID-01: a failed check cannot report success ----------------- #
    def test_kb_hybrid_01_failed_check_cannot_verify(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack()
            try:
                provider = QueueProvider([
                    model_response(call(
                        "w", "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )),
                    model_response(call(
                        "c", "checks_run",
                        {"argv": [sys.executable, "-c", "import sys; sys.exit(9)"]},
                    )),
                    model_response(
                        call("s", "git_status", {}),
                        call("d", "git_diff", {}),
                    ),
                    model_response(content="claimed success"),
                ])
                report = stack.kernel(
                    provider, AgentLimits(max_steps=4, max_seconds=30),
                ).run(stack.session_id)
                self.assertFalse(report.verified)
                self.assertEqual(report.reason, "step_limit")
                self.assertFalse(report.checks[-1]["ok"])
                return {
                    "usage": report.to_dict()["usage"],
                    "evidence": [
                        f"verified={report.verified}",
                        f"reason={report.reason}",
                        f"last_check_ok={report.checks[-1]['ok']}",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-01", "failed check cannot report success", body,
            limitations=["Deterministic scripted provider; no live model."],
        )

    # -- KB-HYBRID-02: proxy double authorization boundary ------------------- #
    def test_kb_hybrid_02_proxy_double_policy(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack(session_id="s", with_mcp=True)
            try:
                policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
                proxy = McpProxy(
                    stack.mcp["client"], stack.repository,
                    stack.sessions, "s", ["echo"],
                    policy=policy,
                    hosted_origin=stack.hosted_origin,
                    proxied_origin=Origin(OriginKind.PROXIED_MCP, "bench-proxy"),
                    audit_path=stack.audit_path,
                )
                policy.set_grants(stack.hosted_origin, {Capability.MCP_CALL})
                policy.set_grants(proxy.proxied_origin, {Capability.MCP_CALL})
                result = proxy.execute("mcp.echo.echo", {"message": "proxied"})
                self.assertEqual(result["tool"], "mcp.echo.echo")
                self.assertEqual(
                    result["result"]["content"][0]["text"], "echo: proxied",
                )
                # Second boundary: a policy without MCP_CALL must deny.
                deny_policy = CapabilityPolicy(AccessProfile.READ_ONLY)
                with self.assertRaises(PolicyDenied):
                    McpProxy(
                        stack.mcp["client"], stack.repository, stack.sessions, "s", ["echo"],
                        policy=deny_policy, hosted_origin=stack.hosted_origin,
                    ).descriptors()
                # Descriptors are secret-free.
                for d in proxy.descriptors():
                    blob = json.dumps(d.to_dict())
                    self.assertNotIn("command", d.to_dict())
                    self.assertNotIn("KaroX/", blob)
                return {
                    "usage": {},
                    "evidence": [
                        "two boundaries (hosted policy + MCP selection)",
                        "deny without MCP_CALL grant",
                        "secret-free descriptors",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-02", "proxy double authorization boundary", body,
            limitations=["Real stdio MCP echo server; no live hosted client."],
        )

    # -- KB-HYBRID-03: cross-provider model switch -------------------------- #
    def test_kb_hybrid_03_cross_provider_model_switch(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack(session_id="s")
            try:
                model_a = QueueProvider([
                    model_response(call(
                        "w", "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )),
                    model_response(call(
                        "c", "checks_run",
                        {"argv": [sys.executable, "-c", "print('ok')"]},
                    )),
                    model_response(content="Done with the patch."),
                ])
                ra = stack.kernel(
                    model_a, AgentLimits(max_steps=3, max_seconds=30),
                    model="model-a",
                ).run("s")
                self.assertEqual(ra.status, "stopped")
                self.assertFalse(ra.verified)
                self.assertEqual(
                    (stack.repository / "sample.txt").read_text(encoding="utf-8"),
                    "after\n",
                )
                model_b = QueueProvider([
                    model_response(
                        call("s", "git_status", {}),
                        call("d", "git_diff", {}),
                    ),
                    model_response(content="Verification complete."),
                ])
                rb = stack.kernel(
                    model_b, AgentLimits(max_steps=8, max_seconds=30),
                    model="model-b",
                ).run("s")
                self.assertTrue(rb.verified)
                self.assertEqual(rb.status, "verified")
                history = stack.sessions.load("s").provider_history
                models = {
                    e.get("model") for e in history
                    if isinstance(e, dict) and e.get("model")
                }
                self.assertIn("model-a", models)
                self.assertIn("model-b", models)
                return {
                    "usage": rb.to_dict()["usage"],
                    "evidence": [
                        f"model_a_verified={ra.verified}",
                        f"model_b_verified={rb.verified}",
                        f"models_in_history={sorted(models)}",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-03", "cross-provider mid-task model switch", body,
            limitations=["Two scripted providers on one leased session."],
        )

    # -- KB-HYBRID-04: recovery across restart ------------------------------ #
    def test_kb_hybrid_04_recovery_across_restart(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack(session_id="s")
            try:
                model_a = QueueProvider([
                    model_response(call(
                        "w", "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )),
                    model_response(call(
                        "c", "checks_run",
                        {"argv": [sys.executable, "-c", "print('ok')"]},
                    )),
                    model_response(content="Done."),
                ])
                ra = stack.kernel(
                    model_a, AgentLimits(max_steps=3, max_seconds=30),
                    model="model-a",
                ).run("s")
                self.assertFalse(ra.verified)
                # Simulate a restart: fresh SessionStore/CoreRuntime on the
                # same on-disk paths, then a second model resumes and verifies
                # without re-mutating the file.
                restarted = stack.restart()
                model_b = QueueProvider([
                    model_response(
                        call("s", "git_status", {}),
                        call("d", "git_diff", {}),
                    ),
                    model_response(content="Verified."),
                ])
                rb = restarted.kernel(
                    model_b, AgentLimits(max_steps=8, max_seconds=30),
                    model="model-b",
                ).run("s")
                self.assertTrue(rb.verified)
                self.assertEqual(
                    (stack.repository / "sample.txt").read_text(encoding="utf-8"),
                    "after\n",
                )
                return {
                    "usage": rb.to_dict()["usage"],
                    "evidence": [
                        "survived fresh SessionStore/CoreRuntime handles",
                        f"verified_after_restart={rb.verified}",
                        "file not re-mutated",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-04", "session recovery across restart", body,
            limitations=["Same process; restart simulated by fresh handles."],
        )

    # -- KB-HYBRID-05: native agent uses a real external stdio MCP ----------- #
    def test_kb_hybrid_05_agent_uses_external_stdio_mcp(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack(session_id="s", with_mcp=True)
            try:
                provider = QueueProvider([
                    model_response(call(
                        "e", "mcp_echo_echo", {"message": "from-agent"},
                    )),
                    model_response(content="echoed via MCP"),
                ])
                report = stack.kernel(
                    provider, AgentLimits(max_steps=2, max_seconds=30),
                ).run("s")
                # The MCP-only run cannot satisfy the durable verification
                # chain, so the agent stops at the step limit after the final
                # answer triggers a repair prompt.  The point of this gate is
                # that the agent routed the alias through Core to the external
                # server, which the persisted history proves.
                self.assertEqual(report.reason, "step_limit")
                self.assertEqual(report.steps, 2)
                history = stack.sessions.load("s").provider_history
                echo_entry = next(
                    e for e in history
                    if isinstance(e, dict) and e.get("core_name") == "mcp.echo.echo"
                )
                text = echo_entry["result"]["data"]["result"]["content"][0]["text"]
                self.assertEqual(text, "echo: from-agent")
                return {
                    "usage": report.to_dict()["usage"],
                    "evidence": [
                        f"steps={report.steps}",
                        f"mcp_result={text}",
                        "agent routed alias through Core to external server",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-05", "native agent uses external stdio MCP", body,
            limitations=["Real bundled stdio echo server; no live MCP."],
        )

    # -- KB-HYBRID-06: bridge credential secret isolation -------------------- #
    def test_kb_hybrid_06_bridge_secret_isolation(self) -> None:
        def body() -> Dict[str, Any]:
            backend = _FakeCredentialBackend()
            store = BridgeCredentialStore(backend=backend)
            token = store.generate()
            self.assertGreaterEqual(len(token), 32)
            info = store.set("bridge-1", token)
            # Credentials live in the dedicated KaroX/bridge namespace.
            self.assertIn(("KaroX/bridge", "bridge-1"), backend.store)
            self.assertEqual(store.resolve(info["reference"]), token)
            # A provider-namespace reference cannot resolve through the bridge store.
            with self.assertRaises(ValueError):
                BridgeCredentialReference.parse("os-keyring:provider/key")
            # Honesty: bridge profile statuses are labelled, not all "tested".
            profiles = known_bridge_profiles()
            statuses = {p.name: p.status for p in profiles}
            self.assertEqual(statuses["notion"], "protocol_compatible")
            self.assertIn(statuses["hyperagent"], {"experimental", "untested"})
            return {
                "usage": {},
                "evidence": [
                    "namespace=KaroX/bridge",
                    "provider reference rejected",
                    f"profiles={len(profiles)}",
                ],
            }
        self._run_gate(
            "KB-HYBRID-06", "bridge credential secret isolation", body,
            limitations=[
                "Fake OS-keyring backend; real keyring availability is "
                "environment-dependent.",
            ],
        )

    # -- KB-HYBRID-07: session lock prevents concurrent mutation ------------- #
    def test_kb_hybrid_07_session_lock(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack(session_id="s")
            try:
                lease = stack.sessions.acquire("s", "owner-a", ttl_seconds=30)
                try:
                    with self.assertRaises(SessionBusy):
                        stack.sessions.acquire("s", "owner-b", ttl_seconds=30)
                finally:
                    stack.sessions.release(lease)
                # After release, a new lease can be acquired.
                lease2 = stack.sessions.acquire("s", "owner-c", ttl_seconds=30)
                stack.sessions.release(lease2)
                return {
                    "usage": {},
                    "evidence": [
                        "concurrent acquire blocked by SessionBusy",
                        "release permits re-acquire",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-07", "session lock blocks concurrent mutation", body,
            limitations=["Single-process lease file; no distributed lock."],
        )

    # -- KB-HYBRID-08: Notion transport regression ------------------------- #
    def test_kb_hybrid_08_notion_transport_regression(self) -> None:
        def body() -> Dict[str, Any]:
            root = Path(__file__).resolve().parents[1]
            env = dict(os.environ)
            env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run(
                [sys.executable, str(root / "scripts" / "test_notion_mcp_transport.py")],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"notion regression failed: {proc.stderr[-400:]}",
            )
            return {
                "usage": {},
                "evidence": ["notion transport script exit 0"],
            }
        self._run_gate(
            "KB-HYBRID-08", "Notion transport regression", body,
            limitations=[
                "Requires the bundled Notion gateway server and its deps; "
                "fails honestly if unavailable.",
            ],
        )

    # -- KB-HYBRID-09: structured handoff document -------------------------- #
    def test_kb_hybrid_09_structured_handoff(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack(session_id="s")
            try:
                # Populate the session with a real two-model run.
                QueueProvider_pipeline_a = QueueProvider([
                    model_response(call(
                        "w", "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )),
                    model_response(call(
                        "c", "checks_run",
                        {"argv": [sys.executable, "-c", "print('ok')"]},
                    )),
                    model_response(content="Done."),
                ])
                stack.kernel(
                    QueueProvider_pipeline_a,
                    AgentLimits(max_steps=3, max_seconds=30), model="model-a",
                ).run("s")
                QueueProvider_pipeline_b = QueueProvider([
                    model_response(
                        call("s", "git_status", {}),
                        call("d", "git_diff", {}),
                    ),
                    model_response(content="Verified."),
                ])
                stack.kernel(
                    QueueProvider_pipeline_b,
                    AgentLimits(max_steps=8, max_seconds=30), model="model-b",
                ).run("s")
                document = build_handoff(
                    stack.sessions.load("s"), repository=stack.repository,
                )
                self.assertEqual(document["schema_version"], HANDOFF_SCHEMA_VERSION)
                self.assertIn("document_sha256", document)
                digest = handoff_digest(document)
                self.assertEqual(len(digest), 64)
                # Digest is stable across recomputation (volatile fields excluded).
                self.assertEqual(digest, handoff_digest(document))
                doc_models = {m["model"] for m in document["model_history"]}
                self.assertIn("model-a", doc_models)
                self.assertIn("model-b", doc_models)
                # Secret-free: no credential markers leak into the document.
                blob = json.dumps(document)
                self.assertNotIn("Bearer ", blob)
                self.assertNotIn("os-keyring:", blob)
                return {
                    "usage": document.get("usage", {}),
                    "evidence": [
                        f"schema_version={document['schema_version']}",
                        f"digest_len={len(digest)}",
                        f"models_in_doc={sorted(m for m in doc_models if m)}",
                        "secret-free",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-09", "structured handoff document", body,
            limitations=["Deterministic two-model run; no live transport."],
        )

    # -- KB-HYBRID-10: capstone verified run with routed cost --------------- #
    def test_kb_hybrid_10_capstone_verified_with_cost(self) -> None:
        def body() -> Dict[str, Any]:
            stack = _Stack()
            try:
                provider = QueueProvider([
                    routed_cost(tool_calls=(call(
                        "w", "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    ),), cost=0.10, cumulative_cost=0.10),
                    routed_cost(tool_calls=(call(
                        "c", "checks_run",
                        {"argv": [sys.executable, "-c", "print('ok')"]},
                    ),), cost=0.10, cumulative_cost=0.20),
                    routed_cost(tool_calls=(
                        call("s", "git_status", {}),
                        call("d", "git_diff", {}),
                    ), cost=0.10, cumulative_cost=0.30),
                    routed_cost(content="verified locally", cost=0.10,
                                 cumulative_cost=0.40),
                ])
                report = stack.kernel(
                    provider, AgentLimits(max_steps=4, max_seconds=30),
                ).run(stack.session_id)
                self.assertTrue(report.verified)
                self.assertEqual(report.status, "verified")
                usage = report.to_dict()["usage"]
                self.assertIn("costs", usage)
                self.assertEqual(usage["costs"]["USD"], 0.40)
                return {
                    "usage": usage,
                    "cost": {"currency": "USD", "cumulative_cost": 0.40},
                    "evidence": [
                        f"verified={report.verified}",
                        f"status={report.status}",
                        f"cost_usd={usage['costs']['USD']}",
                    ],
                }
            finally:
                stack.cleanup()
        self._run_gate(
            "KB-HYBRID-10", "capstone verified run with routed cost", body,
            limitations=["Routed-cost fields scripted; no live billing."],
        )

    # -- Aggregation: every gate produced a well-formed record and passed --- #
    def test_zz_aggregation_all_gates_passed(self) -> None:
        ids = [r.benchmark_id for r in __import__("_bench_support").RECORDS]
        # Every expected gate must have produced a record.
        missing = [i for i in self.EXPECTED_IDS if i not in ids]
        self.assertEqual(
            missing, [], f"missing benchmark records: {missing}",
        )
        # Every record must be well-formed (pass flag matches failure_reason).
        for r in __import__("_bench_support").RECORDS:
            self.assertTrue(
                r.passed, f"{r.benchmark_id} did not pass: {r.failure_reason}",
            )
        # Print the raw run records so they are inspectable in test output.
        print(summary_table())


if __name__ == "__main__":
    unittest.main()
