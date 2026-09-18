"""Guarded pre-release publication behind the stable hosted hot-action surface.

The public hosted schema intentionally stays fixed. A release is still a real
external commit point, so this module verifies the exact candidate, CI run and
tag first, then consumes one fresh chat approval bound to that exact action
before it pushes a tag. The tag is the only mutation performed here;
``prerelease.yml`` owns PyPI and GitHub pre-release publication afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from . import __version__ as RUNTIME_VERSION
from .core import InvalidCommand
from .models import AccessProfile, EvidenceRecord
from .sessions import SessionError
from .task_state import FactOrigin, TaskFact, TaskStateStore

_CHAT_APPROVAL_FACT = "chat_user_approval"
_CHAT_APPROVAL_EVIDENCE = ("user.chat.explicit_approval",)
_CHAT_APPROVAL_MAX_AGE_SECONDS = 300.0
_PRERELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:a|b|rc|dev)\d+(?:[.+-].*)?$")
_TAG = re.compile(r"^v[0-9A-Za-z][0-9A-Za-z._-]{0,126}$")
_GITHUB_HTTPS_REMOTE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_GITHUB_SSH_REMOTE = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$")


class ReleaseActionError(InvalidCommand):
    """A guarded pre-release action could not prove its safety contract."""


def _evidence(**kwargs: Any) -> dict[str, Any]:
    """Return JSON-safe evidence for the hosted hot-command result envelope."""
    return EvidenceRecord(**kwargs).to_dict()


def _creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def _run(repository: Path, argv: list[str], deadline_seconds: float) -> subprocess.CompletedProcess[str]:
    timeout = max(1.0, min(float(deadline_seconds), 60.0))
    kwargs: dict[str, Any] = {
        "cwd": repository,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "check": False,
    }
    flags = _creationflags()
    if flags:
        kwargs["creationflags"] = flags
    try:
        return subprocess.run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseActionError(f"cannot run {argv[0]} for guarded release validation") from exc


def _text(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "").strip()


def _git(repository: Path, *arguments: str, deadline_seconds: float) -> subprocess.CompletedProcess[str]:
    return _run(repository, ["git", *arguments], deadline_seconds)


def _require_success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise ReleaseActionError(f"{label} failed")


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseActionError(f"release action requires non-empty {name}")
    value = value.strip()
    if len(value) > 240 or any(ord(ch) < 33 or ch.isspace() for ch in value):
        raise ReleaseActionError(f"release action {name} is invalid")
    return value


def _required_run_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("ci_run_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseActionError("release action ci_run_id must be a positive integer")
    return value


def _head(repository: Path, deadline_seconds: float) -> str:
    result = _git(repository, "rev-parse", "HEAD", deadline_seconds=deadline_seconds)
    _require_success(result, "Git HEAD lookup")
    head = _text(result)
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReleaseActionError("release action cannot prove the current Git HEAD")
    return head


def _github_slug(remote_url: str) -> str:
    for pattern in (_GITHUB_HTTPS_REMOTE, _GITHUB_SSH_REMOTE):
        match = pattern.fullmatch(remote_url.strip())
        if match is not None:
            return f"{match.group(1)}/{match.group(2)}"
    raise ReleaseActionError("guarded prerelease publication requires a github.com remote")


def _validate_candidate(
    repository: Path,
    *,
    tag: str,
    remote: str,
    ci_run_id: int,
    deadline_seconds: float,
) -> dict[str, Any]:
    expected_tag = f"v{RUNTIME_VERSION}"
    if not _PRERELEASE_VERSION.fullmatch(RUNTIME_VERSION):
        raise ReleaseActionError("guarded prerelease publication refuses a stable runtime version")
    if not _TAG.fullmatch(tag) or tag != expected_tag:
        raise ReleaseActionError(f"release tag must exactly match runtime version: {expected_tag}")
    if not (repository / f"RELEASE_NOTES_{tag}.md").is_file():
        raise ReleaseActionError(f"missing release notes for {tag}")
    if not (repository / ".github" / "workflows" / "prerelease.yml").is_file():
        raise ReleaseActionError("prerelease workflow is missing")

    head = _head(repository, deadline_seconds)
    remotes = _git(repository, "remote", deadline_seconds=deadline_seconds)
    _require_success(remotes, "Git remote enumeration")
    configured = {line.strip() for line in _text(remotes).splitlines() if line.strip()}
    if remote not in configured:
        raise ReleaseActionError("release remote is not configured in this repository")
    remote_url = _git(
        repository,
        "remote",
        "get-url",
        remote,
        deadline_seconds=deadline_seconds,
    )
    _require_success(remote_url, "release remote URL lookup")
    github_slug = _github_slug(_text(remote_url))

    ref_check = _git(
        repository,
        "check-ref-format",
        f"refs/tags/{tag}",
        deadline_seconds=deadline_seconds,
    )
    _require_success(ref_check, "release tag validation")

    dirty = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        deadline_seconds=deadline_seconds,
    )
    _require_success(dirty, "tracked workspace check")
    if _text(dirty):
        raise ReleaseActionError("tracked workspace must be clean before publishing a prerelease")

    existing = _git(
        repository,
        "ls-remote",
        "--tags",
        remote,
        f"refs/tags/{tag}",
        deadline_seconds=deadline_seconds,
    )
    _require_success(existing, "remote tag lookup")
    remote_tag_target: str | None = None
    existing_text = _text(existing)
    if existing_text:
        first = existing_text.splitlines()[0].split("\t", 1)[0].strip()
        if not re.fullmatch(r"[0-9a-f]{40}", first):
            raise ReleaseActionError("remote tag lookup returned an invalid object id")
        remote_tag_target = first
        if remote_tag_target != head:
            raise ReleaseActionError(f"remote tag {tag} points at a different commit")

    ci = _run(
        repository,
        [
            "gh",
            "run",
            "view",
            str(ci_run_id),
            "--repo",
            github_slug,
            "--json",
            "headSha,status,conclusion,event",
        ],
        deadline_seconds,
    )
    _require_success(ci, "GitHub CI lookup")
    try:
        ci_payload = json.loads(ci.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ReleaseActionError("GitHub CI lookup returned invalid JSON") from exc
    if not isinstance(ci_payload, dict):
        raise ReleaseActionError("GitHub CI lookup returned an invalid record")
    if ci_payload.get("headSha") != head:
        raise ReleaseActionError("CI run does not belong to the current Git HEAD")
    if ci_payload.get("status") != "completed" or ci_payload.get("conclusion") != "success":
        raise ReleaseActionError("CI run is not completed successfully")
    if ci_payload.get("event") != "push":
        raise ReleaseActionError("release requires a successful push-triggered CI run")

    return {
        "tag": tag,
        "remote": remote,
        "head": head,
        "ci_run_id": ci_run_id,
        "ci_status": "success",
        "runtime_version": RUNTIME_VERSION,
        "github_repository": github_slug,
        "remote_tag_target": remote_tag_target,
    }


def _validate_main_lineage(
    repository: Path,
    *,
    remote: str,
    source_branch: str,
    source_sha: str,
    deadline_seconds: float,
) -> dict[str, Any]:
    if source_branch != "main":
        raise ReleaseActionError("release lineage integration only accepts source branch main")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ReleaseActionError("release lineage source_sha must be a full Git commit id")

    head = _head(repository, deadline_seconds)
    dirty = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        deadline_seconds=deadline_seconds,
    )
    _require_success(dirty, "tracked workspace check")
    if _text(dirty):
        raise ReleaseActionError("tracked workspace must be clean before integrating main lineage")

    remotes = _git(repository, "remote", deadline_seconds=deadline_seconds)
    _require_success(remotes, "Git remote enumeration")
    configured = {line.strip() for line in _text(remotes).splitlines() if line.strip()}
    if remote not in configured:
        raise ReleaseActionError("release remote is not configured in this repository")

    remote_head = _git(
        repository,
        "ls-remote",
        "--heads",
        remote,
        f"refs/heads/{source_branch}",
        deadline_seconds=deadline_seconds,
    )
    _require_success(remote_head, "remote main lookup")
    remote_text = _text(remote_head)
    if not remote_text:
        raise ReleaseActionError("remote main branch was not found")
    observed_sha = remote_text.splitlines()[0].split("\t", 1)[0].strip()
    if observed_sha != source_sha:
        raise ReleaseActionError("remote main moved; refresh the exact source_sha before integrating")

    known = _git(repository, "cat-file", "-e", f"{source_sha}^{{commit}}", deadline_seconds=deadline_seconds)
    _require_success(known, "remote main commit lookup")
    ancestor = _git(
        repository,
        "merge-base",
        "--is-ancestor",
        source_sha,
        "HEAD",
        deadline_seconds=deadline_seconds,
    )
    if ancestor.returncode == 0:
        return {
            "head": head,
            "source_branch": source_branch,
            "source_sha": source_sha,
            "remote": remote,
            "already_integrated": True,
        }
    if ancestor.returncode != 1:
        raise ReleaseActionError("cannot determine whether main lineage is already integrated")
    return {
        "head": head,
        "source_branch": source_branch,
        "source_sha": source_sha,
        "remote": remote,
        "already_integrated": False,
    }


def _approval_digest(expected: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(expected), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _consume_chat_approval(
    runtime: Any,
    *,
    workstream_id: str,
    expected: Mapping[str, Any],
) -> None:
    store = TaskStateStore(runtime.sessions)
    state = store.load_optional(runtime.session_id, workstream_id=workstream_id)
    if state is None:
        raise ReleaseActionError("release approval workstream does not exist")
    repository_fact = state.facts.get("repository")
    if (
        repository_fact is None
        or not isinstance(repository_fact.value, str)
        or Path(repository_fact.value).expanduser().resolve()
        != Path(runtime.repository).expanduser().resolve()
    ):
        raise ReleaseActionError("release approval workstream belongs to a different repository")
    revision_fact = state.facts.get("repository_revision")
    if revision_fact is None or revision_fact.value != expected.get("head"):
        raise ReleaseActionError("release approval workstream is not bound to the current Git HEAD")
    approval = state.facts.get(_CHAT_APPROVAL_FACT)
    if approval is None or approval.origin is not FactOrigin.REPORTED_BY_AGENT:
        raise ReleaseActionError("fresh explicit chat approval is required for release publication")
    if tuple(approval.evidence) != _CHAT_APPROVAL_EVIDENCE:
        raise ReleaseActionError("release chat approval evidence is invalid")
    age = time.time() - float(approval.recorded_at)
    if age < -30.0 or age > _CHAT_APPROVAL_MAX_AGE_SECONDS:
        raise ReleaseActionError("release chat approval expired")
    value = approval.value
    if not isinstance(value, Mapping) or value.get("approved") is not True:
        raise ReleaseActionError("release publication was not approved in chat")
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ReleaseActionError("release chat approval does not match the exact action")

    consumed = dict(value)
    consumed.update(
        {
            "approved": False,
            "consumed": True,
            "action_digest": _approval_digest(expected),
        }
    )
    try:
        store.checkpoint(
            runtime.session_id,
            {
                _CHAT_APPROVAL_FACT: TaskFact(
                    consumed,
                    FactOrigin.VERIFIED,
                    evidence=("release.chat_approval.consume",),
                )
            },
            expected_revision=state.revision,
            workstream_id=workstream_id,
        )
    except SessionError as exc:
        raise ReleaseActionError("release chat approval became stale before consumption") from exc


def execute_release_action(
    runtime: Any,
    action: str,
    payload: dict[str, Any],
    deadline_seconds: float,
) -> dict[str, Any]:
    """Integrate release lineage or validate/publish one exact pre-release tag."""
    if getattr(runtime, "_access_profile", None) is not AccessProfile.ELEVATED:
        raise ReleaseActionError("guarded release actions require Advanced access")
    if action not in {
        "release.integrate_main_lineage",
        "release.check_prerelease",
        "release.publish_prerelease",
    }:
        raise ReleaseActionError(f"unsupported guarded release action: {action}")

    if action == "release.integrate_main_lineage":
        allowed = {"remote", "source_branch", "source_sha"}
        unknown = sorted(set(payload).difference(allowed))
        if unknown:
            raise ReleaseActionError("release action contains unsupported fields: " + ", ".join(unknown))
        remote = _required_string(payload, "remote")
        source_branch = _required_string(payload, "source_branch")
        source_sha = _required_string(payload, "source_sha")
        lineage = _validate_main_lineage(
            runtime.repository,
            remote=remote,
            source_branch=source_branch,
            source_sha=source_sha,
            deadline_seconds=deadline_seconds,
        )
        if lineage["already_integrated"]:
            return {
                **lineage,
                "integrated": True,
                "merge_created": False,
                "reconciled": True,
                "evidence": [
                    _evidence(
                        kind="release_lineage",
                        summary=f"Main lineage {source_sha[:12]} already integrated",
                        artifact_sha256=lineage["head"],
                        metadata={"remote": remote, "source_branch": source_branch},
                    )
                ],
            }
        merged = _git(
            runtime.repository,
            "merge",
            "--no-ff",
            "-s",
            "ours",
            source_sha,
            "-m",
            f"release: integrate {remote}/{source_branch} lineage for KaroX {RUNTIME_VERSION}",
            deadline_seconds=deadline_seconds,
        )
        if merged.returncode != 0:
            raise ReleaseActionError("guarded main-lineage merge failed")
        merged_head = _head(runtime.repository, deadline_seconds)
        parents = _git(
            runtime.repository,
            "rev-list",
            "--parents",
            "-n",
            "1",
            "HEAD",
            deadline_seconds=deadline_seconds,
        )
        _require_success(parents, "merge-parent verification")
        parent_ids = _text(parents).split()
        if len(parent_ids) != 3 or parent_ids[2] != source_sha:
            raise ReleaseActionError("main-lineage merge did not record the exact remote main parent")
        return {
            **lineage,
            "head": merged_head,
            "integrated": True,
            "merge_created": True,
            "reconciled": False,
            "evidence": [
                _evidence(
                    kind="release_lineage",
                    summary=f"Integrated {remote}/{source_branch} lineage at {source_sha[:12]}",
                    command=["git", "merge", "--no-ff", "-s", "ours", source_sha],
                    exit_code=0,
                    artifact_sha256=merged_head,
                    metadata={"remote": remote, "source_branch": source_branch, "source_sha": source_sha},
                )
            ],
        }

    allowed = {"tag", "remote", "ci_run_id", "workstream_id"}
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise ReleaseActionError("release action contains unsupported fields: " + ", ".join(unknown))

    tag = _required_string(payload, "tag")
    remote = _required_string(payload, "remote")
    workstream_id = _required_string(payload, "workstream_id")
    ci_run_id = _required_run_id(payload)
    candidate = _validate_candidate(
        runtime.repository,
        tag=tag,
        remote=remote,
        ci_run_id=ci_run_id,
        deadline_seconds=deadline_seconds,
    )
    existing_target = candidate.get("remote_tag_target")
    if action == "release.check_prerelease":
        reconciled = existing_target == candidate["head"]
        return {
            **candidate,
            "ready": True,
            "published": reconciled,
            "tag_pushed": False,
            "reconciled": reconciled,
            "evidence": [
                _evidence(
                    kind="release_check",
                    summary=f"Validated prerelease candidate {tag}",
                    artifact_sha256=candidate["head"],
                    metadata={
                        "remote": remote,
                        "tag": tag,
                        "ci_run_id": ci_run_id,
                        "already_published": reconciled,
                    },
                )
            ],
        }

    if existing_target == candidate["head"]:
        return {
            **candidate,
            "ready": True,
            "published": True,
            "tag_pushed": False,
            "reconciled": True,
            "workflow_expected": "prerelease.yml",
            "evidence": [
                _evidence(
                    kind="release_publish",
                    summary=f"Reconciled existing prerelease tag {tag}",
                    artifact_sha256=candidate["head"],
                    metadata={"remote": remote, "tag": tag, "reconciled": True},
                )
            ],
        }

    expected = {
        "action_kind": "release.publish",
        "remote": remote,
        "tag": tag,
        "head": candidate["head"],
        "ci_run_id": ci_run_id,
    }
    _consume_chat_approval(runtime, workstream_id=workstream_id, expected=expected)
    pushed = _git(
        runtime.repository,
        "push",
        "--porcelain",
        remote,
        f"HEAD:refs/tags/{tag}",
        deadline_seconds=deadline_seconds,
    )
    confirmed = _git(
        runtime.repository,
        "ls-remote",
        "--tags",
        remote,
        f"refs/tags/{tag}",
        deadline_seconds=deadline_seconds,
    )
    if confirmed.returncode != 0:
        raise ReleaseActionError("guarded prerelease tag push could not be reconciled")
    confirmed_text = _text(confirmed)
    confirmed_target = confirmed_text.splitlines()[0].split("\t", 1)[0].strip() if confirmed_text else ""
    if confirmed_target != candidate["head"]:
        raise ReleaseActionError("guarded prerelease tag push was not confirmed on the remote")
    return {
        **candidate,
        "remote_tag_target": candidate["head"],
        "ready": True,
        "published": True,
        "tag_pushed": pushed.returncode == 0,
        "reconciled": pushed.returncode != 0,
        "workflow_expected": "prerelease.yml",
        "evidence": [
            _evidence(
                kind="release_publish",
                summary=f"Published prerelease tag {tag}",
                command=["git", "push", "--porcelain", remote, f"HEAD:refs/tags/{tag}"],
                exit_code=pushed.returncode,
                artifact_sha256=candidate["head"],
                metadata={
                    "remote": remote,
                    "tag": tag,
                    "ci_run_id": ci_run_id,
                    "reconciled": pushed.returncode != 0,
                },
            )
        ],
    }
