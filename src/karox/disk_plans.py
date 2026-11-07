"""Cleanup plans: schema, persistence, risk and apply."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from .paths import runtime_dir

from .disk_protection import (
    CleanupError,
    _norm,
    _is_within,
    _suppress_os_errors,
    _validate_relative_target,
    _protected_reason,
)
from .disk_scan import (
    CleanupTarget,
    _human_bytes,
    _snapshot_target,
    _classify_target,
)


PLAN_SCHEMA_VERSION = 1

PLAN_TTL_SECONDS = 20 * 60

MAX_PLAN_TARGETS = 32

_PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")

@dataclass(frozen=True)
class CleanupPlan:
    schema_version: int
    plan_id: str
    root: str
    created_at: float
    expires_at: float
    targets: tuple[CleanupTarget, ...]
    total_bytes: int
    file_count: int
    directory_count: int
    affected_apps: tuple[str, ...]
    code_projects: tuple[str, ...]
    has_user_data: bool
    irreversible: bool = True
    confirmation_required: bool = True

    def to_dict(self, *, public: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload["targets"] = [asdict(item) for item in self.targets]
        if public:
            # Summarize the *whole* frozen set before truncating target details;
            # otherwise a risky ninth target could disappear from the warning.
            risks = {item.data_risk for item in self.targets}
            payload["risk_level"] = (
                "personal_or_code"
                if risks.intersection({"user_data", "code"})
                else "application_data"
                if "application_data" in risks
                else "rebuildable"
            )
            payload["target_count"] = len(self.targets)
            # Fingerprints are useful only to the apply boundary. Keeping them
            # out of model-facing output removes noise without weakening safety.
            for item in payload["targets"]:
                item.pop("fingerprint", None)
            payload["targets"] = payload["targets"][:8]
            payload["targets_truncated"] = len(self.targets) > 8
        return payload

def cleanup_plan_risk(plan: Mapping[str, Any]) -> str:
    """Strongest user-facing data class in a public cleanup plan."""

    declared = str(plan.get("risk_level") or "")
    if declared in {"rebuildable", "application_data", "personal_or_code"}:
        return declared
    raw_targets = plan.get("targets")
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
        return "personal_or_code"
    risks = {
        str(item.get("data_risk") or "user_data")
        for item in raw_targets
        if isinstance(item, Mapping)
    }
    if "user_data" in risks or "code" in risks:
        return "personal_or_code"
    if "application_data" in risks:
        return "application_data"
    return "rebuildable"

def cleanup_impact_preview(plan: Mapping[str, Any], *, language: str = "ru") -> str:
    """Render the frozen deletion impact in four short, model-free lines."""

    english = language == "en"
    raw_targets = plan.get("targets")
    try:
        target_count = max(0, int(plan.get("target_count") or 0))
    except (TypeError, ValueError, OverflowError):
        target_count = 0
    if not target_count:
        target_count = (
            len(raw_targets)
            if isinstance(raw_targets, Sequence) and not isinstance(raw_targets, (str, bytes))
            else 0
        )
    try:
        file_count = max(0, int(plan.get("file_count") or 0))
    except (TypeError, ValueError, OverflowError):
        file_count = 0
    apps = [str(item) for item in (plan.get("affected_apps") or ()) if str(item).strip()]
    projects = [str(item) for item in (plan.get("code_projects") or ()) if str(item).strip()]
    risk = cleanup_plan_risk(plan)

    lines = [
        (
            f"Delete: {_human_bytes(plan.get('total_bytes'))} · {file_count} files · {target_count} targets"
            if english
            else f"Удалится: {_human_bytes(plan.get('total_bytes'))} · {file_count} файлов · {target_count} целей"
        )
    ]
    affected: list[str] = []
    if apps:
        affected.append(("apps: " if english else "приложения: ") + ", ".join(apps[:5]))
    if projects:
        affected.append(("projects: " if english else "проекты: ") + ", ".join(projects[:4]))
    if affected:
        lines.append(("Affects: " if english else "Затронет: ") + " · ".join(affected))
    risk_words = {
        "rebuildable": (
            "Risk: rebuildable caches/dependencies",
            "Риск: восстанавливаемые кэши/зависимости",
        ),
        "application_data": (
            "Risk: application data; local app state may reset",
            "Риск: данные приложений; локальное состояние может сброситься",
        ),
        "personal_or_code": (
            "Risk: code or personal data",
            "Риск: код или пользовательские данные",
        ),
    }[risk]
    lines.append(risk_words[0 if english else 1])
    lines.append("Permanent. Continue?" if english else "Необратимо. Продолжить?")
    return "\n".join(lines)

def _plans_dir() -> Path:
    path = runtime_dir() / "cleanup_plans"
    path.mkdir(parents=True, exist_ok=True)
    return path

def _plan_path(plan_id: str) -> Path:
    if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
        raise CleanupError("invalid cleanup plan id")
    return _plans_dir() / f"{plan_id}.json"

def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def create_cleanup_plan(
    root: str | Path,
    paths: Sequence[str],
    *,
    protected_roots: Sequence[str | Path] = (),
    ttl_seconds: float = PLAN_TTL_SECONDS,
) -> dict[str, Any]:
    """Freeze exact deletion metadata; this function never deletes anything."""

    workspace = Path(root).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise CleanupError("cleanup workspace is not a directory")
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)) or not paths:
        raise CleanupError("cleanup plan requires at least one target")
    if len(paths) > MAX_PLAN_TARGETS:
        raise CleanupError(f"cleanup plan supports at most {MAX_PLAN_TARGETS} targets")
    protected = tuple(Path(item).expanduser().resolve(strict=False) for item in protected_roots)
    deadline = time.monotonic() + 30.0
    targets: list[CleanupTarget] = []
    seen: set[str] = set()
    for raw in paths:
        target = _validate_relative_target(workspace, str(raw))
        key = _norm(target)
        if key in seen:
            continue
        seen.add(key)
        reason = _protected_reason(workspace, target, protected_roots=protected)
        if reason is not None:
            raise CleanupError(f"cleanup target is protected ({reason}): {raw}")
        snapshot = _snapshot_target(target, deadline=deadline)
        category, apps, code_project, data_risk = _classify_target(target, workspace)
        targets.append(
            CleanupTarget(
                path=target.relative_to(workspace).as_posix(),
                kind=snapshot.kind,
                size_bytes=snapshot.size_bytes,
                file_count=snapshot.file_count,
                directory_count=snapshot.directory_count,
                fingerprint=snapshot.fingerprint,
                category=category,
                affected_apps=apps,
                code_project=code_project,
                data_risk=data_risk,
            )
        )
    if not targets:
        raise CleanupError("cleanup plan contains no distinct targets")
    # A parent target already includes a child target. Refusing overlap keeps the
    # action digest obvious and avoids a plan that succeeds or fails depending on
    # deletion order.
    absolute_targets = [workspace / item.path for item in targets]
    for index, left in enumerate(absolute_targets):
        for right in absolute_targets[index + 1 :]:
            if _is_within(left, right) or _is_within(right, left):
                raise CleanupError("cleanup plan contains overlapping targets")

    now = time.time()
    try:
        ttl = min(max(float(ttl_seconds), 60.0), PLAN_TTL_SECONDS)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CleanupError("cleanup plan TTL must be a number") from exc
    apps = tuple(dict.fromkeys(app for item in targets for app in item.affected_apps))
    projects = tuple(dict.fromkeys(item.code_project for item in targets if item.code_project))
    plan = CleanupPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=uuid.uuid4().hex,
        root=str(workspace),
        created_at=now,
        expires_at=now + ttl,
        targets=tuple(targets),
        total_bytes=sum(item.size_bytes for item in targets),
        file_count=sum(item.file_count for item in targets),
        directory_count=sum(item.directory_count for item in targets),
        affected_apps=apps,
        code_projects=projects,
        has_user_data=any(item.data_risk in {"user_data", "application_data"} for item in targets),
    )
    _atomic_json(_plan_path(plan.plan_id), plan.to_dict(public=False))
    return plan.to_dict(public=True)

def _plan_from_payload(payload: dict[str, Any]) -> CleanupPlan:
    if int(payload.get("schema_version", -1)) != PLAN_SCHEMA_VERSION:
        raise CleanupError("unsupported cleanup plan version")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise CleanupError("cleanup plan targets are invalid")
    try:
        targets = tuple(CleanupTarget(**dict(item)) for item in raw_targets if isinstance(item, dict))
        return CleanupPlan(
            schema_version=PLAN_SCHEMA_VERSION,
            plan_id=str(payload["plan_id"]),
            root=str(payload["root"]),
            created_at=float(payload["created_at"]),
            expires_at=float(payload["expires_at"]),
            targets=targets,
            total_bytes=int(payload["total_bytes"]),
            file_count=int(payload["file_count"]),
            directory_count=int(payload["directory_count"]),
            affected_apps=tuple(str(item) for item in payload.get("affected_apps", ())),
            code_projects=tuple(str(item) for item in payload.get("code_projects", ())),
            has_user_data=bool(payload.get("has_user_data")),
            irreversible=bool(payload.get("irreversible", True)),
            confirmation_required=bool(payload.get("confirmation_required", True)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CleanupError("cleanup plan is malformed") from exc

def load_cleanup_plan(plan_id: str, *, public: bool = False) -> dict[str, Any]:
    path = _plan_path(plan_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CleanupError("cleanup plan does not exist or already completed") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupError("cleanup plan cannot be read") from exc
    if not isinstance(payload, dict):
        raise CleanupError("cleanup plan is malformed")
    return _plan_from_payload(payload).to_dict(public=public)

def _remove_tree(path: Path) -> None:
    def make_writable(func: Any, target: str, exc: BaseException) -> None:
        del exc
        with _suppress_os_errors():
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        func(target)

    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    # Python 3.13 supports onexc; onerror remains for compatibility with older
    # runtimes used by downstream packagers. Resolve dynamically because older
    # typeshed versions do not yet expose the 3.13 keyword even when the runtime
    # does, and diagnostics should not fail solely on a typing-version mismatch.
    rmtree: Any = shutil.rmtree
    try:
        rmtree(path, onexc=make_writable)
    except TypeError:  # pragma: no cover - compatibility path
        def onerror(func: Any, target: str, info: Any) -> None:
            make_writable(func, target, info[1])
        rmtree(path, onerror=onerror)

def _append_audit(payload: dict[str, Any]) -> None:
    path = runtime_dir() / "cleanup_audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")

def apply_cleanup_plan(
    plan_id: str,
    *,
    root: str | Path,
    confirmed_by_user: bool,
    protected_roots: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Apply one frozen plan after an explicit human-facing confirmation."""

    if confirmed_by_user is not True:
        raise CleanupError("cleanup apply requires explicit user confirmation")
    stored = load_cleanup_plan(plan_id, public=False)
    plan = _plan_from_payload(stored)
    workspace = Path(root).expanduser().resolve(strict=True)
    if _norm(workspace) != _norm(Path(plan.root)):
        raise CleanupError("cleanup plan belongs to a different workspace")
    if time.time() > plan.expires_at:
        with _suppress_os_errors():
            _plan_path(plan.plan_id).unlink()
        raise CleanupError("cleanup plan expired; scan and review again")
    protected = tuple(Path(item).expanduser().resolve(strict=False) for item in protected_roots)

    # Revalidate every target *before* moving the first one. This is the safety
    # property the user reviewed: same target set, same metadata fingerprint.
    validated: list[tuple[CleanupTarget, Path]] = []
    for item in plan.targets:
        target = _validate_relative_target(workspace, item.path)
        reason = _protected_reason(workspace, target, protected_roots=protected)
        if reason is not None:
            raise CleanupError(f"cleanup target became protected ({reason}): {item.path}")
        snapshot = _snapshot_target(target)
        if snapshot.fingerprint != item.fingerprint:
            raise CleanupError(f"cleanup target changed after preview: {item.path}")
        validated.append((item, target))

    free_before = int(shutil.disk_usage(workspace).free)
    staging_root = workspace / ".karox-cleanup-staging" / plan.plan_id
    if staging_root.exists():
        raise CleanupError("cleanup staging path already exists")
    staging_root.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        for index, (item, target) in enumerate(validated):
            destination = staging_root / f"{index:03d}-{target.name}"
            os.replace(target, destination)
            moved.append((target, destination))
    except Exception as exc:
        rollback_errors: list[str] = []
        for original, staged in reversed(moved):
            try:
                os.replace(staged, original)
            except OSError as rollback_exc:
                rollback_errors.append(f"{original}: {type(rollback_exc).__name__}")
        with _suppress_os_errors():
            staging_root.rmdir()
            staging_root.parent.rmdir()
        detail = ""
        if rollback_errors:
            detail = "; rollback incomplete: " + ", ".join(rollback_errors[:5])
        raise CleanupError("could not stage every target for deletion" + detail) from exc

    removed = 0
    cleanup_failures: list[str] = []
    for _original, staged in moved:
        try:
            _remove_tree(staged)
            removed += 1
        except Exception as exc:
            cleanup_failures.append(f"{staged.name}: {type(exc).__name__}")
    with _suppress_os_errors():
        staging_root.rmdir()
        staging_root.parent.rmdir()
    free_after = int(shutil.disk_usage(workspace).free)

    status = "completed" if not cleanup_failures else "partial"
    audit = {
        "timestamp": time.time(),
        "plan_id": plan.plan_id,
        "root": str(workspace),
        "status": status,
        "target_count": len(plan.targets),
        "removed_target_count": removed,
        "planned_bytes": plan.total_bytes,
        "free_space_delta_bytes": free_after - free_before,
        "affected_apps": list(plan.affected_apps),
        "code_project_count": len(plan.code_projects),
        "has_user_data": plan.has_user_data,
        "failures": cleanup_failures[:10],
    }
    _append_audit(audit)
    with _suppress_os_errors():
        _plan_path(plan.plan_id).unlink()
    return {
        **audit,
        "deleted_paths": [item.path for item in plan.targets[:8]],
        "deleted_paths_truncated": len(plan.targets) > 8,
        "quarantine_remaining": bool(cleanup_failures),
    }
