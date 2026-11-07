"""Safe, bounded disk-cleanup planning with a user-owned apply boundary.

The model is deliberately not given a delete primitive.  It may inspect compact
metadata with :func:`scan_cleanup_candidates` and freeze an exact plan with
:func:`create_cleanup_plan`.  Only the human-facing TUI/CLI confirmation path
calls :func:`apply_cleanup_plan` afterwards.

That split is load-bearing:

* a model cannot manufacture ``confirmed=True`` in a tool call because apply is
  not a model tool;
* the plan records an exact metadata fingerprint and expires quickly, so a path
  that changed after the user reviewed it is refused rather than deleted;
* Windows/system/KaroX runtime locations are blocked even when the selected
  workspace is a whole drive such as ``C:\\``;
* scan results contain names, sizes, categories and impact labels, never file
  contents, which keeps both privacy exposure and token use bounded.
"""

from __future__ import annotations

from .disk_protection import (  # noqa: F401
    CleanupError as CleanupError,
    _WINDOWS_PROTECTED_NAMES as _WINDOWS_PROTECTED_NAMES,
    _WINDOWS_PROTECTED_FILES as _WINDOWS_PROTECTED_FILES,
    _norm as _norm,
    _is_within as _is_within,
    is_drive_root as is_drive_root,
    _environment_system_roots as _environment_system_roots,
    _suppress_os_errors as _suppress_os_errors,
    workspace_system_reason as workspace_system_reason,
    _karox_protected_roots as _karox_protected_roots,
    _is_reparse_boundary as _is_reparse_boundary,
    _validate_relative_target as _validate_relative_target,
    _protected_reason as _protected_reason,
)
from .disk_scan import (  # noqa: F401
    TargetSnapshot as TargetSnapshot,
    CleanupTarget as CleanupTarget,
    _human_bytes as _human_bytes,
    _PROJECT_MARKERS as _PROJECT_MARKERS,
    _REBUILDABLE_CATEGORIES as _REBUILDABLE_CATEGORIES,
    _snapshot_target as _snapshot_target,
    _project_root as _project_root,
    _affected_apps as _affected_apps,
    _classify_target as _classify_target,
    _candidate_payload as _candidate_payload,
    _known_candidate_paths as _known_candidate_paths,
    scan_cleanup_candidates as scan_cleanup_candidates,
    MAX_SCAN_CANDIDATES as MAX_SCAN_CANDIDATES,
    MAX_SCAN_DIRECTORIES as MAX_SCAN_DIRECTORIES,
    MAX_SNAPSHOT_ENTRIES as MAX_SNAPSHOT_ENTRIES,
    DEFAULT_SCAN_SECONDS as DEFAULT_SCAN_SECONDS,
    DEFAULT_MIN_CANDIDATE_BYTES as DEFAULT_MIN_CANDIDATE_BYTES,
)
from .disk_plans import (  # noqa: F401
    PLAN_SCHEMA_VERSION as PLAN_SCHEMA_VERSION,
    PLAN_TTL_SECONDS as PLAN_TTL_SECONDS,
    MAX_PLAN_TARGETS as MAX_PLAN_TARGETS,
    _PLAN_ID as _PLAN_ID,
    CleanupPlan as CleanupPlan,
    cleanup_plan_risk as cleanup_plan_risk,
    cleanup_impact_preview as cleanup_impact_preview,
    _plans_dir as _plans_dir,
    _plan_path as _plan_path,
    _atomic_json as _atomic_json,
    create_cleanup_plan as create_cleanup_plan,
    _plan_from_payload as _plan_from_payload,
    load_cleanup_plan as load_cleanup_plan,
    _remove_tree as _remove_tree,
    _append_audit as _append_audit,
    apply_cleanup_plan as apply_cleanup_plan,
)




# Whole OS/application roots are never cleanup targets.  User-local application
# caches (for example Chrome's Cache under AppData) are intentionally *not* in
# this list and are handled by the impact classifier instead.








































































__all__ = [
    "CleanupError",
    "CleanupPlan",
    "CleanupTarget",
    "DEFAULT_MIN_CANDIDATE_BYTES",
    "MAX_PLAN_TARGETS",
    "MAX_SCAN_CANDIDATES",
    "PLAN_TTL_SECONDS",
    "apply_cleanup_plan",
    "cleanup_impact_preview",
    "cleanup_plan_risk",
    "create_cleanup_plan",
    "is_drive_root",
    "load_cleanup_plan",
    "scan_cleanup_candidates",
    "workspace_system_reason",
]