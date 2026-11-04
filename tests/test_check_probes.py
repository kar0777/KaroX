"""Reproduction probes next to the approved checks (plan 4.2.2, 2026-11-04).

In the sandbox SWE runs an exact rule such as ``python run_public_tests.py``
refused every ``python -c`` reproduction, and the model spent its steps on git
and file reads instead. The same interpreter may now run a snippet, a test
module or a repository script through checks.run; none of it is evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest

from karox import cli
from karox.action_execution import CapabilityCoreRuntime
from karox.action_policy import ActionDecisionEngine
from karox.agent import AgentKernel, AgentLimits, ContextBudget, SYSTEM_PROMPT
from karox.core import CoreCommand, CoreRuntime, InvalidCommand, VerificationRule
from karox.core_process import PROBE_MODULES, probe_python_command
from karox.core_types import VerificationCommandNotApproved
from karox.engine_tool_env import CoreToolEnvironment
from karox.models import AccessProfile, CoreResult, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.project_context import render_environment
from karox.risk_engine import RiskEngine
from karox.sessions import SessionStore

PY = sys.executable
RULES = (VerificationRule.parse([PY, "run_public_tests.py"]),)


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "run_public_tests.py").write_text("print('public ok')\n", encoding="utf-8")
    (repo / "tests" / "runtests.py").write_text(
        "import sys\nprint('ran', *sys.argv[1:])\n", encoding="utf-8"
    )
    (repo / "pkg.py").write_text("VALUE = 41\n", encoding="utf-8")
    _git(repo, "init", "-q")
    return repo


# -- admission ---------------------------------------------------------------


def test_inline_code_with_the_approved_interpreter_is_a_probe(repository: Path) -> None:
    probe = probe_python_command(RULES, [PY, "-c", "print(1)", "x"], repository)
    assert probe is not None and probe.form == "inline"
    assert probe.code == "print(1)" and probe.tail == ("x",)


def test_allowlisted_modules_and_nothing_else(repository: Path) -> None:
    assert "pip" not in PROBE_MODULES and "http.server" not in PROBE_MODULES
    probe = probe_python_command(RULES, [PY, "-m", "pytest", "tests", "-q"], repository)
    assert probe is not None and probe.form == "module"
    assert probe_python_command(RULES, [PY, "-m", "pip", "install", "x"], repository) is None
    assert probe_python_command(RULES, [PY, "-m", "http.server"], repository) is None


def test_repository_scripts_only(repository: Path, tmp_path: Path) -> None:
    script = probe_python_command(RULES, [PY, "tests/runtests.py", "a.b"], repository)
    assert script is not None and script.form == "script"
    absolute = probe_python_command(
        RULES, [PY, str(repository / "tests" / "runtests.py")], repository
    )
    assert absolute is not None
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    assert probe_python_command(RULES, [PY, "../outside.py"], repository) is None
    assert probe_python_command(RULES, [PY, str(outside)], repository) is None
    assert probe_python_command(RULES, [PY, "missing.py"], repository) is None
    assert probe_python_command(RULES, [PY, "pkg.txt"], repository) is None
    hooks = repository / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "x.py").write_text("print('hook')\n", encoding="utf-8")
    assert probe_python_command(RULES, [PY, ".git/hooks/x.py"], repository) is None


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlink")
def test_a_symlink_out_of_the_repository_is_refused(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.py"
    outside.write_text("print('elsewhere')\n", encoding="utf-8")
    (repository / "link.py").symlink_to(outside)
    assert probe_python_command(RULES, [PY, "link.py"], repository) is None


def test_other_interpreters_options_and_non_python_rules_are_refused(
    repository: Path,
) -> None:
    assert probe_python_command(RULES, ["python9", "-c", "print(1)"], repository) is None
    assert probe_python_command(RULES, [PY, "-X", "dev", "-c", "1"], repository) is None
    assert probe_python_command(RULES, [PY, "-c", "   "], repository) is None
    assert probe_python_command(RULES, [PY, "-m", "pytest", "*"], repository) is None
    assert (
        probe_python_command(RULES, [PY, "-m", "pytest", "-c", "x.ini"], repository)
        is None
    )
    npm = (VerificationRule.parse(["npm", "test"]),)
    assert probe_python_command(npm, ["npm", "-c", "x"], repository) is None
    assert probe_python_command(npm, [PY, "-c", "print(1)"], repository) is None


# -- runtime -----------------------------------------------------------------


def _runtime(repository: Path, tmp_path: Path, *, probes: bool) -> tuple[CapabilityCoreRuntime, Origin]:
    store = SessionStore(tmp_path / "sessions")
    store.create(repository, "fix it", AccessProfile.WORKSPACE_WRITE, session_id="session-1")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    origin = Origin(OriginKind.NATIVE_AGENT, "probe-test")
    policy.set_grants(origin, cli._agent_grants(AccessProfile.WORKSPACE_WRITE, "build"))
    risk = RiskEngine()
    core = CapabilityCoreRuntime(
        repository,
        policy,
        store,
        tmp_path / "audit.jsonl",
        risk=risk,
        action_decisions=ActionDecisionEngine(risk),
        verification_commands=[[PY, "run_public_tests.py"]],
        allow_probes=probes,
    )
    return core, origin


def _check(core: CapabilityCoreRuntime, origin: Origin, argv: list[str], key: str) -> CoreResult:
    store = core.sessions
    lease = store.acquire("session-1", "test", ttl_seconds=30.0)
    try:
        return core.execute(
            CoreCommand(
                "checks.run",
                {"argv": argv},
                "session-1",
                origin,
                idempotency_key=key,
                deadline_seconds=60.0,
            ),
            lease=lease,
        )
    finally:
        store.release(lease)


def test_a_probe_runs_reports_itself_and_is_never_evidence(
    repository: Path, tmp_path: Path
) -> None:
    core, origin = _runtime(repository, tmp_path, probes=True)
    assert core.probe_interpreters == (PY,)

    inline = _check(core, origin, [PY, "-c", "import pkg; print(pkg.VALUE + 1)"], "p1")
    assert inline.ok and "42" in inline.data["stdout"]
    assert inline.data["probe"] is True
    assert inline.data["verification_eligible"] is False
    assert [item.kind for item in inline.evidence] == ["probe"]
    assert inline.evidence[0].summary.startswith("Probe passed: ")

    script = _check(core, origin, [PY, "tests/runtests.py", "template_tests"], "p2")
    assert script.ok and "ran template_tests" in script.data["stdout"]
    assert script.data["probe"] is True and script.data["verification_eligible"] is False

    failed = _check(core, origin, [PY, "-c", "raise SystemExit(3)"], "p3")
    assert not failed.ok and failed.data["exit_code"] == 3
    assert failed.evidence[0].summary.startswith("Probe failed: ")

    approved = _check(core, origin, [PY, "run_public_tests.py"], "c1")
    assert approved.ok and approved.data["verification_eligible"] is True
    assert "probe" not in approved.data
    assert [item.kind for item in approved.evidence] == ["check"]

    record = core.sessions.load("session-1")
    assert [entry.get("probe", False) for entry in record.checks] == [True, True, True, False]


def test_without_probes_the_exact_rule_still_refuses(repository: Path, tmp_path: Path) -> None:
    core, origin = _runtime(repository, tmp_path, probes=False)
    assert core.probe_interpreters == ()
    with pytest.raises(VerificationCommandNotApproved) as refused:
        _check(core, origin, [PY, "-c", "print(1)"], "n1")
    assert "probe" not in str(refused.value)


def test_a_refusal_names_the_probe_forms(repository: Path, tmp_path: Path) -> None:
    core, origin = _runtime(repository, tmp_path, probes=True)
    with pytest.raises(VerificationCommandNotApproved) as refused:
        _check(core, origin, [PY, "-m", "pip", "install", "requests"], "r1")
    message = str(refused.value)
    assert "never verification evidence" in message and "-m pytest" in message


def test_background_checks_refuse_probes(repository: Path, tmp_path: Path) -> None:
    core, _origin = _runtime(repository, tmp_path, probes=True)
    with pytest.raises(InvalidCommand, match="checks.start runs only"):
        core._prepare_checks_start({"argv": [PY, "tests/runtests.py"]})


# -- what the model is told ------------------------------------------------


def test_environment_text_does_not_contradict_probes(repository: Path) -> None:
    plain = render_environment(repository, verification_commands=[[PY, "run_public_tests.py"]])
    assert "Only those commands may run as a check" in plain
    with_probes = render_environment(
        repository, verification_commands=[[PY, "run_public_tests.py"]], probes=(PY,)
    )
    assert "Only those commands count as verification" in with_probes
    assert "may run as a check" not in with_probes


class _NoProvider:
    def provider_name(self) -> str:
        return "none"

    def complete(self, request):  # pragma: no cover - never called
        raise AssertionError("not called")

    def close(self) -> None:
        return None


def _kernel(repository: Path, tmp_path: Path, *, probes: bool) -> AgentKernel:
    store = SessionStore(tmp_path / "kernel-sessions")
    origin = Origin(OriginKind.NATIVE_AGENT, "probe-kernel")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    core = partial(CoreRuntime, allow_probes=probes)(
        repository,
        policy,
        store,
        tmp_path / "kernel-audit.jsonl",
        verification_commands=[(PY, "run_public_tests.py")],
    )
    return AgentKernel(
        provider=_NoProvider(),
        model="test-model",
        core=core,
        sessions=store,
        origin=origin,
        limits=AgentLimits(max_steps=4, max_seconds=30.0),
        system_prompt=SYSTEM_PROMPT,
        context=ContextBudget(),
    )


def test_the_engine_contract_names_probes_only_when_enabled(
    repository: Path, tmp_path: Path
) -> None:
    enabled = _kernel(repository, tmp_path, probes=True).implementation.system_prompt
    assert "Probes: checks.run also runs" in enabled
    assert f"'{PY} -c CODE'" in enabled
    assert "count as verification evidence" in enabled
    disabled = _kernel(repository, tmp_path, probes=False).implementation.system_prompt
    assert "Probes:" not in disabled
    assert "may be executed and used as verification evidence" in disabled


def test_a_probe_result_is_labelled_in_the_observation() -> None:
    from types import SimpleNamespace

    env = SimpleNamespace(config=SimpleNamespace(output_char_limit=20_000))
    result = CoreResult(
        ok=True,
        command="checks.run",
        data={"exit_code": 0, "stdout": "42\n", "stderr": "", "probe": True},
        correlation_id="c-1",
    )
    output = CoreToolEnvironment._render_output(env, "checks.run", result)["output"]
    assert output.startswith("[checks.run exit 0 · probe (not verification evidence)]")


def test_agent_run_parser_has_an_opt_out() -> None:
    parser = cli._parser()
    args = parser.parse_args(["agent", "run", "--task", "x", "--no-probes"])
    assert args.no_probes is True
    args = parser.parse_args(["agent", "run", "--task", "x"])
    assert args.no_probes is False
