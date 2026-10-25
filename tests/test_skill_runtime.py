"""Per-session skill selection, execution and audit.

The catalogue in :mod:`karox.skills` already discovers bounded metadata and
loads recipe text.  This file covers the session half: explicit and automatic
activation, disabling, scoped script execution behind the capability policy,
and the audit trail that makes every invocation answerable after the fact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from karox.core import CoreCommand, CoreRuntime, InvalidCommand, SessionError
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.sessions import SessionStore
from karox.skill_runtime import SkillScriptDenied, SkillSession, SkillSessionRuntime
from karox.skills import SkillCatalog, SkillError


SCRIPT = """import json, os, sys
print(json.dumps({
    "cwd": os.getcwd(),
    "skill_dir": os.environ.get("KAROX_SKILL_DIR", ""),
    "args": sys.argv[1:],
}))
"""


def write_skill(
    root: Path,
    name: str = "demo-skill",
    *,
    triggers: Optional[List[str]] = None,
    scripts: Optional[List[str]] = None,
    script_files: Optional[Dict[str, str]] = None,
) -> Path:
    directory = root / ".karox" / "skills" / name
    directory.mkdir(parents=True)
    frontmatter = [
        "---",
        f"name: {name}",
        "description: A demonstration recipe for tests.",
        "version: 1.0.0",
    ]
    if triggers:
        frontmatter.append(f"triggers: {json.dumps(triggers)}")
    if scripts is not None:
        frontmatter.append(f"scripts: {json.dumps(scripts)}")
    frontmatter.append('files: ["notes.md"]')
    frontmatter.append("---")
    (directory / "SKILL.md").write_text(
        "\n".join(frontmatter) + "\n\nDo the demo thing.\n", encoding="utf-8"
    )
    (directory / "notes.md").write_text("reference note\n", encoding="utf-8")
    for relative, body in (script_files or {"scripts/check.py": SCRIPT}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        target.write_text(body, encoding="utf-8")
    return directory


def session_policy(
    profile: AccessProfile, grants: List[Capability]
) -> Tuple[CapabilityPolicy, Origin]:
    policy = CapabilityPolicy(profile)
    origin = Origin(OriginKind.NATIVE_AGENT, "cli-test-session")
    policy.set_grants(origin, grants)
    return policy, origin


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    write_skill(
        tmp_path,
        triggers=["failing test", "pytest"],
        scripts=["scripts/check.py"],
    )
    return tmp_path


def make_session(
    repo: Path,
    tmp_path: Path,
    *,
    policy: Optional[CapabilityPolicy] = None,
    origin: Optional[Origin] = None,
    disabled: Tuple[str, ...] = (),
) -> SkillSession:
    return SkillSession(
        SkillCatalog(repo, global_directory=tmp_path / "global", include_builtin=False),
        "sess-1",
        policy=policy,
        invoker_origin=origin,
        disabled=disabled,
        scratch_root=tmp_path / "scratch",
        audit_path=tmp_path / "audit.jsonl",
    )


# --------------------------------------------------------------------- scripts


def test_discovery_reports_declared_scripts(repo: Path, tmp_path: Path) -> None:
    catalog = SkillCatalog(repo, global_directory=tmp_path / "global", include_builtin=False)
    metadata = catalog.discover()[0]
    assert metadata.scripts == ("scripts/check.py",)
    assert metadata.to_dict()["scripts"] == ["scripts/check.py"]


def test_script_declarations_with_bad_extensions_reject_the_skill(
    tmp_path: Path,
) -> None:
    write_skill(tmp_path, name="bad-skill", scripts=["tools/run.exe"])
    catalog = SkillCatalog(tmp_path, global_directory=tmp_path / "global", include_builtin=False)
    catalog.discover()
    rejected = [
        item.name
        for item in catalog.diagnostics
        if item.status == "rejected" and item.name == "bad-skill"
    ]
    assert rejected == ["bad-skill"]
    with pytest.raises(SkillError, match="not found"):
        catalog.get("bad-skill")


def test_scripts_outside_the_scripts_directory_reject_the_skill(
    tmp_path: Path,
) -> None:
    write_skill(tmp_path, name="escape-skill", scripts=["../escape.py"])
    catalog = SkillCatalog(tmp_path, global_directory=tmp_path / "global", include_builtin=False)
    catalog.discover()
    assert [
        item.name
        for item in catalog.diagnostics
        if item.status == "rejected" and item.name == "escape-skill"
    ] == ["escape-skill"]


# ------------------------------------------------------------------ activation


def test_explicit_activation_is_audited_and_bounded(repo: Path, tmp_path: Path) -> None:
    session = make_session(repo, tmp_path)
    activation = session.activate("demo-skill", mode="explicit", reason="model asked")
    assert "guidance only" in activation.prompt
    assert "karox-skill" in session.active_prompt()
    events = [record.event for record in session.audit_log()]
    assert events.count("activated") == 1
    record = session.audit_log()[0]
    assert record.mode == "explicit"
    assert record.identity == "demo-skill@1.0.0"

    with pytest.raises(SkillError, match="already active"):
        session.activate("demo-skill", mode="explicit", reason="again")


def test_disabled_skills_are_refused_everywhere(repo: Path, tmp_path: Path) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(
        repo, tmp_path, policy=policy, origin=origin, disabled=("demo-skill",)
    )
    with pytest.raises(SkillError, match="disabled"):
        session.activate("demo-skill", mode="explicit", reason="model asked")
    with pytest.raises(SkillError, match="disabled"):
        session.run_script("demo-skill", "scripts/check.py", origin=origin)
    assert session.automatic_candidates("fix the failing test with pytest") == ()
    assert session.activate_automatic("fix the failing test with pytest") == ()


def test_automatic_selection_matches_declared_triggers(
    repo: Path, tmp_path: Path
) -> None:
    session = make_session(repo, tmp_path)
    candidates = session.automatic_candidates(
        "please investigate the failing test in the pytest suite"
    )
    assert [item.metadata.name for item in candidates] == ["demo-skill"]
    assert candidates[0].matched_triggers

    activations = session.activate_automatic(
        "please investigate the failing test in the pytest suite"
    )
    assert len(activations) == 1
    record = [r for r in session.audit_log() if r.event == "activated"][0]
    assert record.mode == "automatic"
    assert "failing test" in record.reason


def test_automatic_activation_is_capped(repo: Path, tmp_path: Path) -> None:
    write_skill(repo, name="second-skill", triggers=["pytest"], scripts=["scripts/check.py"])
    session = make_session(repo, tmp_path)
    session.max_auto_activated = 1
    activations = session.activate_automatic("run the pytest thing")
    assert len(activations) == 1


def test_activation_never_touches_the_capability_policy(
    repo: Path, tmp_path: Path
) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.REPO_READ]
    )
    grants_before = {k: set(v) for k, v in policy.origin_grants.items()}
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    session.activate("demo-skill", mode="explicit", reason="model asked")
    assert {k: set(v) for k, v in policy.origin_grants.items()} == grants_before


# ------------------------------------------------------------------ execution


def test_run_script_executes_with_scoped_cwd_and_env(
    repo: Path, tmp_path: Path
) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    result = session.run_script(
        "demo-skill", "scripts/check.py", ["--flag", "x"], origin=origin
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    skill_dir = repo / ".karox" / "skills" / "demo-skill"
    assert Path(payload["cwd"]) != skill_dir
    assert Path(payload["cwd"]).is_relative_to(tmp_path / "scratch")
    assert payload["skill_dir"] == str(skill_dir)
    assert payload["args"] == ["--flag", "x"]
    evidence = result.evidence()
    assert evidence.kind == "skill_script"
    assert evidence.exit_code == 0
    assert evidence.command and evidence.command[0] == result.command[0]


def test_run_script_denies_undeclared_or_unsafe_scripts(
    repo: Path, tmp_path: Path
) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    with pytest.raises(SkillError, match="does not declare"):
        session.run_script("demo-skill", "scripts/undeclared.py", origin=origin)
    with pytest.raises(SkillError):
        session.run_script("demo-skill", "notes.md", origin=origin)
    with pytest.raises(SkillError):
        session.run_script("demo-skill", "../demo-skill/scripts/check.py", origin=origin)


def test_run_script_refuses_a_directory_declared_as_a_script(tmp_path: Path) -> None:
    write_skill(tmp_path, name="dir-skill", scripts=["scripts/dir.py"])
    skill = tmp_path / ".karox" / "skills" / "dir-skill"
    (skill / "scripts" / "dir.py").mkdir()
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = SkillSession(
        SkillCatalog(tmp_path, global_directory=tmp_path / "global", include_builtin=False),
        "sess-1",
        policy=policy,
        invoker_origin=origin,
        scratch_root=tmp_path / "scratch",
    )
    with pytest.raises(SkillError, match="regular"):
        session.run_script("dir-skill", "scripts/dir.py", origin=origin)


def test_run_script_fails_closed_without_process_capability(
    repo: Path, tmp_path: Path
) -> None:
    policy, origin = session_policy(AccessProfile.READ_ONLY, [Capability.REPO_READ])
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    with pytest.raises(PolicyDenied):
        session.run_script("demo-skill", "scripts/check.py", origin=origin)
    assert [r.event for r in session.audit_log() if r.event == "script_denied"]


def test_run_script_fails_closed_without_a_policy(repo: Path, tmp_path: Path) -> None:
    session = make_session(repo, tmp_path)
    origin = Origin(OriginKind.NATIVE_AGENT, "cli-test-session")
    with pytest.raises(SkillScriptDenied, match="policy"):
        session.run_script("demo-skill", "scripts/check.py", origin=origin)


def test_run_script_refuses_a_manifest_that_changed_after_discovery(
    repo: Path, tmp_path: Path
) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    session.catalog.discover()
    manifest = repo / ".karox" / "skills" / "demo-skill" / "SKILL.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("1.0.0", "2.0.0"),
        encoding="utf-8",
    )
    with pytest.raises(SkillError, match="changed after discovery"):
        session.run_script("demo-skill", "scripts/check.py", origin=origin)


def test_run_script_times_out_and_clips_output(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        name="slow-skill",
        scripts=["scripts/loop.py"],
        script_files={"scripts/loop.py": "print('x' * 300_000)\nimport time\ntime.sleep(30)\n"},
    )
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = SkillSession(
        SkillCatalog(tmp_path, global_directory=tmp_path / "global", include_builtin=False),
        "sess-1",
        policy=policy,
        invoker_origin=origin,
        scratch_root=tmp_path / "scratch",
        script_timeout_seconds=1.0,
    )
    result = session.run_script("slow-skill", "scripts/loop.py", origin=origin)
    assert result.timed_out is True
    assert result.exit_code != 0
    assert len(result.output) <= 50_000 + 200


def test_run_script_refuses_links(repo: Path, tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("creating symlinks on Windows needs elevated privileges")
    skill_dir = repo / ".karox" / "skills" / "demo-skill"
    replacement = skill_dir / "scripts" / "real.py"
    replacement.write_text(SCRIPT, encoding="utf-8")
    target = skill_dir / "scripts" / "check.py"
    target.unlink()
    os.symlink(replacement.name, target)
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    with pytest.raises(SkillError, match="link"):
        session.run_script("demo-skill", "scripts/check.py", origin=origin)


def test_audit_log_is_persisted_as_jsonl(repo: Path, tmp_path: Path) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    session.run_script("demo-skill", "scripts/check.py", origin=origin)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    decoded = [json.loads(line) for line in lines]
    assert {item["event"] for item in decoded} >= {"script_started", "script_finished"}


# ------------------------------------------------------- the real Core runtime


def core_stack(
    repo: Path, tmp_path: Path, profile: AccessProfile, grants: List[Capability]
):
    policy, origin = session_policy(profile, grants)
    store = SessionStore(tmp_path / "sessions")
    record = store.create(repo, "run the demo skill", access_profile=profile)
    session = SkillSession(
        SkillCatalog(repo, global_directory=tmp_path / "global", include_builtin=False),
        record.session_id,
        policy=policy,
        invoker_origin=origin,
        scratch_root=tmp_path / "scratch",
        audit_path=tmp_path / "audit.jsonl",
    )
    runtime = SkillSessionRuntime(
        repo,
        policy,
        store,
        tmp_path / "core-audit.jsonl",
        skill_session=session,
    )
    return runtime, origin, store, record


def test_skill_run_executes_through_the_core_runtime(
    repo: Path, tmp_path: Path
) -> None:
    runtime, origin, store, record = core_stack(
        repo, tmp_path, AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    command = CoreCommand(
        "skill.run",
        {"name": "demo-skill", "script": "scripts/check.py", "args": ["--flag"]},
        record.session_id,
        origin,
        idempotency_key="skill-once",
    )
    lease = store.acquire(record.session_id, "test-skill", ttl_seconds=30.0)
    try:
        first = runtime.execute(command, lease=lease)
    finally:
        store.release(lease)
    assert first.ok is True, first.data
    assert first.data["exit_code"] == 0
    assert first.evidence and first.evidence[0].kind == "skill_script"

    lease = store.acquire(record.session_id, "test-skill", ttl_seconds=30.0)
    try:
        second = runtime.execute(command, lease=lease)
    finally:
        store.release(lease)
    assert second.idempotent_replay is True
    finished = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "script_finished"
    ]
    assert len(finished) == 1


def test_skill_run_is_denied_without_the_capability(
    repo: Path, tmp_path: Path
) -> None:
    runtime, origin, store, record = core_stack(
        repo, tmp_path, AccessProfile.READ_ONLY, [Capability.REPO_READ]
    )
    command = CoreCommand(
        "skill.run",
        {"name": "demo-skill", "script": "scripts/check.py"},
        record.session_id,
        origin,
        idempotency_key="skill-denied",
    )
    lease = store.acquire(record.session_id, "test-skill", ttl_seconds=30.0)
    try:
        with pytest.raises(PolicyDenied):
            runtime.execute(command, lease=lease)
    finally:
        store.release(lease)


def test_skill_run_requires_lease_and_idempotency_key(
    repo: Path, tmp_path: Path
) -> None:
    runtime, origin, store, record = core_stack(
        repo, tmp_path, AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    keyed = CoreCommand(
        "skill.run",
        {"name": "demo-skill", "script": "scripts/check.py"},
        record.session_id,
        origin,
        idempotency_key="lease-required",
    )
    with pytest.raises(SessionError, match="mutation lease"):
        runtime.execute(keyed)

    unkeyed = CoreCommand(
        "skill.run",
        {"name": "demo-skill", "script": "scripts/check.py"},
        record.session_id,
        origin,
    )
    lease = store.acquire(record.session_id, "test-skill", ttl_seconds=30.0)
    try:
        with pytest.raises(InvalidCommand, match="idempotency key"):
            runtime.execute(unkeyed, lease=lease)
    finally:
        store.release(lease)


def test_skill_run_preflight_rejects_undeclared_scripts_before_reservation(
    repo: Path, tmp_path: Path
) -> None:
    runtime, origin, store, record = core_stack(
        repo, tmp_path, AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    command = CoreCommand(
        "skill.run",
        {"name": "demo-skill", "script": "scripts/nope.py"},
        record.session_id,
        origin,
        idempotency_key="skill-preflight",
    )
    lease = store.acquire(record.session_id, "test-skill", ttl_seconds=30.0)
    try:
        with pytest.raises(SkillError, match="does not declare"):
            runtime.execute(command, lease=lease)
    finally:
        store.release(lease)


def test_runtime_tool_definition_is_mutating_and_scoped(
    repo: Path, tmp_path: Path
) -> None:
    runtime, _origin, _store, _record = core_stack(
        repo, tmp_path, AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    assert isinstance(runtime, CoreRuntime)
    tools = {item.name: item for item in runtime.tools()}
    definition = tools["skill.run"]
    assert definition.capability is Capability.PROCESS_RUN
    assert definition.mutates is True
    assert definition.replayable is True
    assert "script" in definition.input_schema["properties"]


def test_script_arguments_are_bounded(repo: Path, tmp_path: Path) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    with pytest.raises(SkillError, match="arguments"):
        session.run_script(
            "demo-skill", "scripts/check.py", ["x" * 4000], origin=origin
        )
    with pytest.raises(SkillError, match="arguments"):
        session.run_script(
            "demo-skill", "scripts/check.py", [f"a{i}" for i in range(40)], origin=origin
        )


def test_result_data_is_json_serializable(repo: Path, tmp_path: Path) -> None:
    policy, origin = session_policy(
        AccessProfile.WORKSPACE_WRITE, [Capability.PROCESS_RUN]
    )
    session = make_session(repo, tmp_path, policy=policy, origin=origin)
    result = session.run_script("demo-skill", "scripts/check.py", origin=origin)
    json.dumps(result.to_dict())
    json.dumps(result.evidence().to_dict())
    for record in session.audit_log():
        json.dumps(record.to_dict())
