"""Additional Core tools needed by an external coding agent.

These tools live in a subclass instead of in :mod:`karox.core` so the audited
boundary file stays untouched.  Every tool still goes through the same
``CoreRuntime.execute`` path, so policy, repository confinement, mutation
leases, idempotency and evidence apply unchanged.

Why each tool exists:

``repo.edit_file``
    ``repo.write_file`` replaces a whole file.  That is unusable for a module of
    several thousand lines, because the caller has to reproduce the entire file
    to change three lines and any transcription slip silently corrupts working
    code.  A bounded, exact-match replacement makes large files editable safely.

``repo.read_lines``
    ``repo.read_file`` returns the whole file.  A windowed read lets a caller
    inspect a specific region of a large module without paying for the rest.

``git.log``
    ``git.status`` and ``git.diff`` describe the working tree but say nothing
    about history, so a caller could not tell what had already been committed.

``web.fetch``
    Every engineer reuses proven code. Without a way to read it, a model can
    only re-derive a known effect, shader or library behaviour from memory and
    ships a worse approximation of something that already exists. This reads one
    source file from a public code source -- into the answer, or into the
    repository -- so the canonical implementation can be adapted instead of
    reinvented. The host list is closed: this is a code reader, not a general
    page fetcher, and nothing is ever uploaded.

The file listing is also overridden to skip dependency and build directories.
The base implementation walks everything, so a virtual environment can consume
the whole result budget and make search useless in a real checkout.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .command_guard import DeveloperCommandBlocked, validate_developer_command_argv
from .core import (
    CoreError,
    CoreRuntime,
    InvalidCommand,
    InvalidPath,
    _path_label,
)
from .edit_engine import ALL, UNIQUE, EditFailure, EditOp, apply_edits
from .hot_worker import hot_worker_supervisor
from .models import CoreCommand, CoreResult, EvidenceRecord
from .security import contains_credential, redact, redact_content
from .sessions import MutationLease, SessionRecord
from .tool_schemas import additional_tool_definitions


# Directories that are never source material.  Matched on any path component,
# so a nested virtual environment is skipped too.  Git and runtime metadata are
# already rejected by CoreRuntime.safe_path and are not repeated here.
# Where the model's view of a long command output is cut. A compiler or a test
# runner states the cause first and repeats it in a summary, so both ends are
# kept and the middle is written to an artifact: what explains the failure stays
# in the conversation, and the whole text stays readable on disk. Core's own
# MAX_OUTPUT_BYTES ceiling still bounds the capture itself; this bounds what a
# turn has to pay for.
OUTPUT_ELISION_TRIGGER_CHARS = 8_000
OUTPUT_ELISION_HEAD_CHARS = 2_000
OUTPUT_ELISION_TAIL_CHARS = 3_000
OUTPUT_ELISION_TOOLS = frozenset({"checks.run", "repo.command", "dev.command"})
_OUTPUT_TEXT_FIELDS = ("stdout", "stderr")


IGNORED_DIRECTORY_NAMES = frozenset(
    {
        # Scratch trees of this repository, which hold copies of it: a search
        # that walks them returns three hits for one definition, and the model
        # cannot tell which one is the code it is changing.
        ".tmp",
        ".work",
        ".mypy_cache",
        ".netlify",
        ".next",
        ".nox",
        ".nuxt",
        ".output",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".tox",
        ".turbo",
        ".venv",
        ".vite",
        ".zcode",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "site-packages",
        "target",
        "venv",
    }
)

_IGNORED_DIRECTORY_SUFFIXES = (".egg-info",)
#: Vendored copies of this project under ``tools/`` are named after the version
#: they adapted, so the rule is a prefix rather than a name.
_IGNORED_DIRECTORY_PREFIXES = ("adapt-v5-", "adapt-v6-")

_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def is_ignored_path(relative: str) -> bool:
    """True when a repository-relative path sits inside an ignored directory."""
    for part in Path(relative).parts[:-1] or ():
        lowered = part.lower()
        if lowered in IGNORED_DIRECTORY_NAMES:
            return True
        if lowered.endswith(_IGNORED_DIRECTORY_SUFFIXES):
            return True
        if lowered.startswith(_IGNORED_DIRECTORY_PREFIXES):
            return True
    return False


# Public code sources. Reading a library or an example implementation from one
# of these is how proven code gets reused. The list is closed rather than
# merely validated, because "read the source of a known library" and "reach any
# host on the internet" are different capabilities: the second one is
# ``network``, which reaches anywhere and stays Advanced-only, while this tool
# holds ``code.fetch`` and must not quietly become the first. Entries are
# domains, so a CDN's own aliases resolve (cdn./fastly./gcore. jsdelivr.net,
# raw. and gist. githubusercontent.com) while a lookalike does not: the suffix
# must follow a dot, so unpkg.com.example.net stays refused.
CODE_SOURCE_HOSTS: tuple[str, ...] = (
    "cdnjs.cloudflare.com",
    "esm.sh",
    "githubusercontent.com",
    "jsdelivr.net",
    "unpkg.com",
)

# Content types a source file may arrive as. A response that is not text is
# refused rather than decoded, so a mis-typed URL cannot return a binary that
# looks like source.
CODE_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        "application/ecmascript",
        "application/javascript",
        "application/json",
        "application/typescript",
        "application/x-javascript",
        "application/x-typescript",
    }
)

FETCH_CHUNK_BYTES = 64 * 1024


def code_source_url(raw: str) -> tuple[str, str]:
    """Validate a public code-source URL and return ``(url, host)``.

    Every refusal names the value that caused it: a model told only "not
    allowed" retries variants of the same rejected URL, while a model shown the
    offending host picks one from the list.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise InvalidCommand("url must be a non-empty string")
    value = raw.strip()
    if len(value) > 2_000:
        raise InvalidCommand(f"url is {len(value)} characters; the limit is 2000")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise InvalidCommand(f"url is not parseable: {_path_label(value)}") from exc
    if parts.scheme != "https":
        raise InvalidCommand(
            "code sources are read over https only; got scheme "
            f"{parts.scheme or '(none)'}"
        )
    if parts.username or parts.password:
        raise InvalidCommand("a code-source URL must not carry credentials")
    if parts.port not in (None, 443):
        raise InvalidCommand(
            f"only the default https port is allowed, not {parts.port}"
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidCommand("url names no host")
    if not any(host == item or host.endswith("." + item) for item in CODE_SOURCE_HOSTS):
        raise InvalidCommand(
            f"host is not a public code source: {host}; allowed: "
            + ", ".join(CODE_SOURCE_HOSTS)
        )
    normalized = value.split("#", 1)[0]
    return normalized, host


def _is_code_source_type(content_type: str) -> bool:
    lowered = content_type.strip().lower()
    return lowered.startswith("text/") or lowered in CODE_SOURCE_TYPES


class _CodeSourceRedirects(HTTPRedirectHandler):
    """Follow a redirect only while it stays on a public code source.

    Re-validating every hop keeps the allowlist a property of what was actually
    read rather than of what was asked for: a source that redirects to an
    arbitrary host is refused at the hop, not discovered after the body.
    """

    max_redirections = 3
    max_repeats = 2

    def __init__(self) -> None:
        super().__init__()
        self.hops = 0

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        self.hops += 1
        if self.hops > self.max_redirections:
            # urllib's own message for this is the original status code, which
            # reads as "the source answered HTTP 302" and sends the caller
            # looking for a server problem instead of a redirect loop.
            raise InvalidCommand(
                f"more than {self.max_redirections} redirects; read the source at "
                "its final location instead"
            )
        code_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _replaced_occurrences(text: str, ops: Sequence[EditOp]) -> int:
    """How many occurrences of the original text this batch replaces.

    Counted for reporting only. A batch that reached this point already applied
    cleanly, so an edit whose exact anchor the file does not contain was matched
    through the normalized retry, which accepts a unique match or nothing.
    """
    total = 0
    for op in ops:
        found = text.count(op.old)
        if found and op.occurrence == ALL:
            total += found
        else:
            total += 1
    return total


def _edit_diff(relative: str, before: str, after: str) -> str:
    """Unified diff of one edit, bounded so it cannot flood a turn."""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            n=3,
        )
    )
    if len(diff) > ExtendedCoreRuntime.EDIT_DIFF_MAX_CHARS:
        return diff[: ExtendedCoreRuntime.EDIT_DIFF_MAX_CHARS] + "…[truncated]"
    return diff


class ExtendedCoreRuntime(CoreRuntime):
    """Core runtime with editing, windowed reads and history inspection."""

    # The base small-tree search fast path reads these attributes dynamically,
    # so dependency/build filtering stays owned by this Extended Core module
    # without creating a core.py -> core_tools.py import cycle.
    SEARCH_IGNORED_DIRECTORY_NAMES = IGNORED_DIRECTORY_NAMES
    SEARCH_IGNORED_DIRECTORY_SUFFIXES = _IGNORED_DIRECTORY_SUFFIXES
    SEARCH_IGNORED_DIRECTORY_PREFIXES = _IGNORED_DIRECTORY_PREFIXES

    MAX_READ_LINES = 1_500
    MAX_EDIT_OCCURRENCES = 200
    MAX_EDIT_BATCH = 20
    EDIT_DIFF_MAX_CHARS = 4_000
    MAX_PLAN_ITEMS = 12
    MAX_PLAN_ITEM_CHARS = 160
    MAX_LOG_ENTRIES = 200
    MAX_LISTED_FILES = 2_000
    MAX_DIFF_PATHS = 100
    # What a fetched source may weigh. A whole library build is worth saving to
    # the repository, but it is not worth putting in the conversation, so the
    # file ceiling is generous and the in-answer ceiling is not.
    MAX_FETCH_BYTES = 1_000_000
    MAX_FETCH_CHARS = 40_000
    FETCH_TIMEOUT_SECONDS = 20.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        maintenance_protected_paths = kwargs.pop("maintenance_protected_paths", ())
        if not isinstance(maintenance_protected_paths, (list, tuple)):
            raise ValueError("maintenance_protected_paths must be a list or tuple")
        self._maintenance_protected_paths = tuple(
            str(Path(item).expanduser().resolve(strict=False))
            for item in maintenance_protected_paths
        )
        # Skill recipes are read through the catalogue that discovered them, so
        # the tools below add no access of their own: the same name validation,
        # directory confinement, byte limits and sha256 identity checks that
        # guard ``SkillCatalog.load`` are the whole surface. A runtime built
        # without a catalogue does not advertise the tools at all, because a
        # tool that can only fail is worse than a tool that is not offered.
        skill_catalog = kwargs.pop("skill_catalog", None)
        self._skill_catalog = skill_catalog
        # A visual check runs in a context of its own: the caller injects the
        # checker (it owns the provider the run is already using), so Core keeps
        # knowing nothing about providers and the tool stays a normal tool that
        # returns a bounded verdict.
        visual_checker = kwargs.pop("visual_checker", None)
        self._visual_checker = visual_checker
        # Background jobs exist for hosted clients, whose calls have a
        # transport deadline; a native agent keeps the synchronous tools, so
        # its tool list (and prompt cache) does not change.
        self._command_jobs = bool(kwargs.pop("command_jobs", False))
        super().__init__(*args, **kwargs)
        handlers = dict(self._handlers)
        handlers["repo.edit_file"] = self._edit_file
        handlers["repo.apply_patch"] = self._apply_patch
        handlers["repo.read_lines"] = self._read_lines
        handlers["repo.inspect"] = self._repo_inspect
        handlers["plan.update"] = self._plan_update
        handlers["disk.scan"] = self._disk_scan
        handlers["disk.plan_cleanup"] = self._disk_plan_cleanup
        handlers["repo.command"] = self._repo_command
        handlers["dev.command"] = self._dev_command
        handlers["tests.run"] = self._tests_run
        handlers["runtime.status"] = self._runtime_status
        handlers["git.log"] = self._git_log
        handlers["web.fetch"] = self._fetch_source
        if self._skill_catalog is not None:
            handlers["skill.list"] = self._skill_list
            handlers["skill.load"] = self._skill_load
            handlers["skill.read"] = self._skill_read
        if self._visual_checker is not None:
            handlers["visual.check"] = self._visual_check
        if self._command_jobs:
            handlers["dev.command_start"] = self._dev_command_start
            handlers["dev.command_status"] = self._dev_command_status
            handlers["dev.command_logs"] = self._dev_command_logs
            handlers["dev.command_cancel"] = self._dev_command_cancel
            handlers["checks.start"] = self._checks_start
            handlers["checks.status"] = self._checks_status
            handlers["checks.logs"] = self._checks_logs
            handlers["checks.cancel"] = self._checks_cancel
        self._handlers = handlers
        additional = additional_tool_definitions(
            max_edit_batch=self.MAX_EDIT_BATCH,
            max_plan_items=self.MAX_PLAN_ITEMS,
            max_plan_item_chars=self.MAX_PLAN_ITEM_CHARS,
            include_skill_tools=self._skill_catalog is not None,
            include_visual_check=self._visual_checker is not None,
            include_command_jobs=self._command_jobs,
        )
        collisions = set(additional).intersection(self._definitions)
        if collisions:
            raise CoreError(
                f"extended tools collide with existing tools: {sorted(collisions)}"
            )
        self._definitions.update(additional)
        # git.diff without a path filter returns the whole working tree diff.
        # In a branch with dozens of touched files that answer is too large to
        # read and crowds out the change actually under review, so accept an
        # explicit path list. The property is optional, so existing callers that
        # pass only `staged` keep working unchanged.
        self._handlers = dict(self._handlers)
        self._handlers["git.diff"] = self._git_diff_paths
        base_diff = self._definitions["git.diff"]
        self._definitions["git.diff"] = replace(
            base_diff,
            description=(
                "Read the current Git diff, optionally limited to given paths."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        )

    # -- repo.inspect -------------------------------------------------------

    def _repo_inspect(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        goal = self._required(arguments, "goal", str).strip()
        if not goal:
            raise InvalidCommand("repo.inspect goal must be a non-empty string")
        depth = arguments.get("depth", "focused")
        if depth not in {"focused", "standard", "deep"}:
            raise InvalidCommand(
                "repo.inspect depth must be focused, standard, or deep"
            )

        from .artifacts import ArtifactStore
        from .repo_context import RepositoryContextEngine

        namespace = "core-repo-inspect-" + hashlib.sha256(
            str(self.repository).encode("utf-8")
        ).hexdigest()[:16]
        engine = RepositoryContextEngine(
            self.repository,
            ArtifactStore(namespace),
            policy_profile=self.policy.profile.value,
        )
        return dict(engine.inspect(goal, str(depth)))

    # -- skill.list / skill.load / skill.read ------------------------------

    def _require_skill_catalog(self) -> Any:
        catalog = self._skill_catalog
        if catalog is None:
            raise CoreError("skill recipes are not available in this run")
        return catalog

    def _skill_list(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del arguments, deadline_seconds
        catalog = self._require_skill_catalog()
        return {
            "skills": [
                {
                    "name": item.name,
                    "description": item.description,
                    "when_to_use": item.when_to_use or "",
                    "source_kind": item.source_kind,
                }
                for item in catalog.discover()
            ]
        }

    def _skill_load(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        catalog = self._require_skill_catalog()
        content = catalog.load(self._required(arguments, "name", str))
        return {
            "name": content.metadata.name,
            "version": content.metadata.version,
            "source_kind": content.metadata.source_kind,
            "content_sha256": content.metadata.content_sha256,
            "instructions": content.instructions,
            # Names and sizes only: the recipe is the payload, and a reference
            # that is not needed this step must not be paid for.
            "files": [
                {"path": path, "bytes": len(value.encode("utf-8"))}
                for path, value in sorted(content.references.items())
            ],
        }

    def _skill_read(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        catalog = self._require_skill_catalog()
        name = self._required(arguments, "name", str)
        path = self._required(arguments, "path", str)
        text, digest = catalog.read_reference(name, path)
        return {
            "name": name,
            "path": path,
            "sha256": digest,
            "characters": len(text),
            "content": text,
        }

    # -- visual.check ------------------------------------------------------

    def _visual_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        """One look at one page, judged in a context that is not this one.

        The page must be one this run wrote, so the path is confined exactly
        like every other repository read. A remote URL is deliberately not
        accepted: fetching an arbitrary host is a network capability the Build
        profile does not hold, and the page a check is about is the page in the
        working tree.
        """

        del deadline_seconds
        checker = self._visual_checker
        if checker is None:
            raise CoreError("visual checks are not available in this run")
        relative = self._required(arguments, "path", str)
        target = self.safe_path(relative)
        if not target.is_file():
            raise FileNotFoundError(target)
        if target.suffix.lower() not in {".html", ".htm"}:
            raise InvalidCommand(
                "visual_check looks at a rendered page; give an .html file"
            )
        goal = arguments.get("goal")
        if goal is not None and not isinstance(goal, str):
            raise InvalidCommand("goal must be a string")
        return dict(checker(target, goal=goal or ""))

    # -- disk maintenance --------------------------------------------------

    def _disk_scan(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        from .disk_maintenance import scan_cleanup_candidates

        max_candidates = arguments.get("max_candidates", 24)
        min_size_mb = arguments.get("min_size_mb", 25.0)
        scan_seconds = arguments.get("scan_seconds", min(8.0, deadline_seconds))
        return scan_cleanup_candidates(
            self.repository,
            max_candidates=int(max_candidates),
            min_size_mb=float(min_size_mb),
            scan_seconds=min(float(scan_seconds), float(deadline_seconds)),
            protected_roots=self._maintenance_protected_paths,
        )

    def _disk_plan_cleanup(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        from .disk_maintenance import create_cleanup_plan

        paths = arguments.get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(item, str) for item in paths
        ):
            raise InvalidCommand("disk.plan_cleanup paths must be a non-empty string array")
        return create_cleanup_plan(
            self.repository,
            paths,
            protected_roots=self._maintenance_protected_paths,
        )

    # -- hot developer worker ---------------------------------------------

    def _apply_patch(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        """First-class unified-patch facade over the transactional repo worker."""
        result = hot_worker_supervisor().execute_repo(
            self,
            {"action": "apply_patch", "payload": dict(arguments)},
            deadline_seconds,
        )
        changed_files = result.get("changed_files")
        if result.get("dry_run") is True:
            result["changed"] = False
        elif isinstance(changed_files, list):
            result["changed"] = bool(changed_files)
        if isinstance(changed_files, list):
            if len(changed_files) == 1 and isinstance(changed_files[0], str):
                result["path"] = changed_files[0]
        return result

    def _repo_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        return hot_worker_supervisor().execute_repo(self, arguments, deadline_seconds)

    def _prepare_dev_command(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> tuple[List[str], float]:
        raw_argv = arguments.get("argv")
        if not isinstance(raw_argv, list):
            raise InvalidCommand("dev.command argv must be an array")
        # Full developer access broadens the local development surface; it does
        # not remove KaroX's global no-push/no-publish/no-auth/no-deploy
        # invariants. Keep one shared guard with durable command jobs so a long
        # command cannot gain powers that the synchronous path would refuse.
        try:
            argv = list(validate_developer_command_argv(raw_argv))
        except DeveloperCommandBlocked as exc:
            raise InvalidCommand(str(exc)) from exc

        raw_timeout = arguments.get("timeout_seconds", deadline_seconds)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise InvalidCommand("dev.command timeout_seconds must be a number")
        timeout = float(raw_timeout)
        if not 0 < timeout <= float(deadline_seconds):
            raise InvalidCommand("dev.command timeout_seconds must be positive and within the request deadline")
        return argv, timeout

    def _dev_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        argv, timeout = self._prepare_dev_command(arguments, deadline_seconds)
        result = self._run(argv, timeout, inherit_environment=True)
        display_argv = result["argv"]
        result["_evidence"] = [
            EvidenceRecord(
                kind="command",
                summary=("Passed" if result.get("exit_code") == 0 else "Failed")
                + f": {' '.join(display_argv)}",
                command=display_argv,
                exit_code=result.get("exit_code"),
                metadata={
                    "timed_out": bool(result.get("timed_out")),
                    "repository_scoped": True,
                    "shell": False,
                },
            )
        ]
        return result

    def _tests_run(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        return hot_worker_supervisor().execute_tests(self, arguments, deadline_seconds)

    # -- background jobs (hosted clients) ----------------------------------

    def _job_store(self) -> Any:
        from .command_jobs import CommandJobStore, repository_job_root
        from .paths import runtime_dir

        return CommandJobStore(repository_job_root(runtime_dir(), self.repository))

    @staticmethod
    def _job_launch_argv(argv: List[str]) -> List[str]:
        from .core_process import _is_karox_module_command, _karox_module_launch
        from .process_launcher import resolve_executable

        source = _karox_module_launch(argv) if _is_karox_module_command(argv) else list(argv)
        return resolve_executable(source)

    def _prepare_dev_command_start(self, arguments: Dict[str, Any]) -> tuple[List[str], float]:
        from .command_jobs import job_timeout

        raw_argv = arguments.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv:
            raise InvalidCommand("dev.command_start argv must be a non-empty array")
        try:
            argv = list(validate_developer_command_argv(raw_argv))
        except DeveloperCommandBlocked as exc:
            raise InvalidCommand(str(exc)) from exc
        return argv, job_timeout(arguments.get("timeout_seconds"))

    def _dev_command_start(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        import os

        del deadline_seconds
        argv, timeout = self._prepare_dev_command_start(arguments)
        # Same environment as dev.command: Full developer access behaves like
        # a terminal this KaroX process opened.
        environment = os.environ.copy()
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        payload = self._job_store().start(
            kind="dev",
            request_id=arguments.get("request_id"),
            argv=argv,
            launch_argv=self._job_launch_argv(argv),
            cwd=self.repository,
            timeout_seconds=timeout,
            environment=environment,
        )
        payload["next"] = "poll dev.command_status or dev.command_logs with this job_id"
        return payload

    def _prepare_checks_start(self, arguments: Dict[str, Any]) -> tuple[List[str], float]:
        from .command_jobs import MAX_TIMEOUT_SECONDS, job_timeout

        plan = self._prepare_check({"argv": arguments.get("argv")}, MAX_TIMEOUT_SECONDS)
        if plan.probe:
            raise InvalidCommand(
                "checks.start runs only the approved verification commands; run "
                "a probe with checks.run"
            )
        if plan.inline_code is not None:
            raise InvalidCommand(
                "checks.start does not run inline interpreter code; write the snippet "
                "to a scratch file in the repository and start '<interpreter> <file>'"
            )
        return list(plan.argv), job_timeout(arguments.get("timeout_seconds"))

    def _checks_start(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        from .core_process import _is_karox_module_command
        from .security import child_process_environment

        del deadline_seconds
        argv, timeout = self._prepare_checks_start(arguments)
        environment = child_process_environment()
        if _is_karox_module_command(argv):
            environment["PYTHONSAFEPATH"] = "1"
        payload = self._job_store().start(
            kind="check",
            request_id=arguments.get("request_id"),
            argv=argv,
            launch_argv=self._job_launch_argv(argv),
            cwd=self.repository,
            timeout_seconds=timeout,
            environment=environment,
        )
        payload["next"] = "poll checks.status or checks.logs with this job_id"
        return payload

    def _job_status(self, kind: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        job_id = arguments.get("job_id")
        store = self._job_store()
        if job_id is None:
            return {"jobs": store.list(kind=kind)}
        return store.status(job_id, kind=kind)

    def _dev_command_status(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_status("dev", arguments)

    def _checks_status(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_status("check", arguments)

    def _job_logs(self, kind: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from .command_jobs import DEFAULT_LOG_LIMIT

        return self._job_store().logs(
            arguments.get("job_id"),
            kind=kind,
            limit=arguments.get("limit", DEFAULT_LOG_LIMIT),
            offset=arguments.get("offset"),
        )

    def _dev_command_logs(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_logs("dev", arguments)

    def _checks_logs(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_logs("check", arguments)

    def _dev_command_cancel(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_store().cancel(arguments.get("job_id"), kind="dev")

    def _checks_cancel(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_store().cancel(arguments.get("job_id"), kind="check")

    def _runtime_status(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        if arguments:
            raise InvalidCommand("runtime.status takes no arguments")
        return hot_worker_supervisor().status()

    def _prepare_apply_patch(self, arguments: Dict[str, Any], deadline_seconds: float) -> None:
        self._prepare_repo_command(
            {"action": "apply_patch", "payload": dict(arguments)}, deadline_seconds
        )

    def _prepare_repo_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> None:
        del deadline_seconds
        action = arguments.get("action")
        payload = arguments.get("payload")
        shellish = sorted(
            key
            for key in ("command", "argv", "cmd", "shell", "script", "scripts")
            if key in arguments or (isinstance(payload, dict) and key in payload)
        )
        if shellish:
            raise InvalidCommand(
                "repo.command is a patch/batch tool, not a shell; it never runs "
                f"commands. Remove {', '.join(shellish)} and use checks.run for the "
                "approved verification, tests.run for extra tests, or dev.command "
                "for a developer command. Batch shape: action=batch with "
                'payload={"operations":[{"op":"write|delete|move|mkdir", ...}]}.'
            )
        if action not in {"apply_patch", "batch"} or not isinstance(payload, dict):
            raise InvalidCommand(
                "repo.command requires action=apply_patch or action=batch with an "
                'object payload. apply_patch takes {"patch": "..."}; batch takes '
                '{"operations": [{"op": "write|delete|move|mkdir", ...}]}. This '
                "tool edits files; it is not a shell. For a command use "
                "checks.run, tests.run, or dev.command."
            )
        try:
            hot_worker_supervisor().validate_repo(arguments)
        except InvalidCommand as exc:
            # The worker already names the offending field. Restating the one
            # canonical shape here stops the next call from being another
            # invented key: the benchmark run sent 37 payload variants, each
            # refused with only the field name it guessed wrong.
            if action == "apply_patch":
                # A patch failure is about hunk counts or one diff; telling the
                # model about the batch shape here sent a 3D run into retrying
                # the wrong form with the wrong guidance.
                raise InvalidCommand(
                    f"{exc} apply_patch takes one unified diff in "
                    'payload.patch; fix the @@ hunk counts instead of changing '
                    'the tool shape. Use action=batch only when you mean '
                    '{"operations": [...]}.'
                ) from exc
            raise InvalidCommand(
                f"{exc} The only batch shape is action=batch with payload="
                '{"operations": [{"op": "write|delete|move|mkdir", ...}], '
                '"dry_run": false}; an operation accepts op, path, content, '
                "source, destination, expected_sha256, missing_ok, overwrite "
                "or allow_secret_literal, depending on op."
            ) from exc
        # The handler builds and validates one complete WorkspaceTransaction
        # before its first filesystem mutation. Running a separate dry-run here
        # created a second transaction, widened the preflight/commit race, and
        # made an immediate idempotent replay re-apply patch context to the
        # already-updated tree before the stored result could be returned.

    @staticmethod
    def _prepare_tests_run(arguments: Dict[str, Any]) -> None:
        if arguments.get("suite", "focused") not in {"focused", "full", "split"}:
            raise InvalidCommand("tests.run suite must be focused, full, or split")

    # -- result honesty ---------------------------------------------------

    def execute(
        self,
        command: CoreCommand,
        capability_token: Optional[str] = None,
        lease: Optional[MutationLease] = None,
    ) -> CoreResult:
        result = super().execute(command, capability_token, lease)
        # git.log is a read-only process call, so the base class does not treat a
        # non-zero exit as a failure. It has no idempotency record, so adjusting
        # the flag here cannot desynchronise a stored replay.
        if command.name in {"git.log", "git.push", "tests.run", "dev.command"} and (
            result.data.get("timed_out") or result.data.get("exit_code") != 0
        ):
            result.ok = False
        # A batch edit whose anchors did not match is a recoverable outcome with
        # repair instructions, not an exception -- so it travels in the result.
        # Its ok flag must still say the file was not changed, or a run could
        # count a refused edit as work done and a metric could miss the failure.
        # The flag is derived from data, not set at the point of return, so a
        # replayed refusal reports the same thing as the first attempt.
        if command.name == "repo.edit_file" and (
            result.data.get("error_code") == "edit_failed"
        ):
            result.ok = False
        self._bound_model_visible_output(command, result)
        return result

    def _bound_model_visible_output(
        self, command: CoreCommand, result: CoreResult
    ) -> None:
        """Keep a long command output to its head and tail, artifact the whole.

        The middle of a test run is repetition: the failing case is named first
        and counted again at the end. Paying input tokens for the rest of it on
        every later turn is the most expensive way to show the model a stack
        trace it already has.
        """

        if command.name not in OUTPUT_ELISION_TOOLS:
            return
        data = result.data if isinstance(result.data, dict) else None
        if data is None:
            return
        for field in _OUTPUT_TEXT_FIELDS:
            text = data.get(field)
            if not isinstance(text, str) or len(text) <= OUTPUT_ELISION_TRIGGER_CHARS:
                continue
            artifact_id = self._store_output_artifact(
                command.session_id, command.name, field, text
            )
            omitted = len(text) - OUTPUT_ELISION_HEAD_CHARS - OUTPUT_ELISION_TAIL_CHARS
            data[field] = (
                text[:OUTPUT_ELISION_HEAD_CHARS]
                + f"\n…[{omitted} chars omitted, artifact {artifact_id}]…\n"
                + text[-OUTPUT_ELISION_TAIL_CHARS:]
            )
            data[f"{field}_artifact_id"] = artifact_id
            # The full length, so a reader can tell how much the middle held and
            # know that the artifact is the whole text rather than the window.
            data[f"{field}_chars"] = len(text)
            data[f"{field}_elided"] = True

    @staticmethod
    def _store_output_artifact(
        session_id: str, core_name: str, field: str, text: str
    ) -> str:
        from .artifacts import ArtifactStore

        record = ArtifactStore(session_id).put(
            text.encode("utf-8"),
            name=f"{core_name}-{field}.txt",
            mime="text/plain",
        )
        return record.artifact_id

    def _preflight_mutation(
        self,
        command_name: str,
        arguments: Dict[str, Any],
        deadline_seconds: float,
    ) -> None:
        if command_name == "repo.edit_file":
            self._prepare_edit(arguments)
            return
        if command_name == "repo.apply_patch":
            self._prepare_apply_patch(arguments, deadline_seconds)
            return
        if command_name == "plan.update":
            self._prepare_plan_update(arguments)
            return
        if command_name == "repo.command":
            self._prepare_repo_command(arguments, deadline_seconds)
            return
        if command_name == "dev.command":
            self._prepare_dev_command(arguments, deadline_seconds)
            return
        if command_name == "dev.command_start":
            self._prepare_dev_command_start(arguments)
            return
        if command_name == "checks.start":
            self._prepare_checks_start(arguments)
            return
        if command_name == "tests.run":
            self._prepare_tests_run(arguments)
            return
        if command_name == "web.fetch":
            self._prepare_fetch(arguments)
            return
        super()._preflight_mutation(command_name, arguments, deadline_seconds)

    def _record_mutation(
        self, record: SessionRecord, command: CoreCommand, result: CoreResult
    ) -> None:
        super()._record_mutation(record, command, result)
        if command.name == "plan.update":
            # Re-validated from the arguments rather than echoed through the
            # result: the model just sent this list, and sending it back would
            # cost the request it was meant to save.
            record.plan_items = self._prepare_plan_update(command.arguments)
            return
        if command.name in {"repo.command", "repo.apply_patch"}:
            if result.data.get("dry_run") is True:
                return
            for changed_path in result.data.get("changed_files", []):
                if isinstance(changed_path, str) and changed_path not in record.changed_files:
                    record.changed_files.append(changed_path)
            for operation in result.data.get("operations", []) or []:
                if not isinstance(operation, dict) or operation.get("changed") is not True:
                    continue
                op_path = operation.get("path")
                if not isinstance(op_path, str):
                    continue
                if operation.get("op") == "write" and operation.get("previous_sha256") is None:
                    if op_path not in record.created_files:
                        record.created_files.append(op_path)
                elif operation.get("op") == "delete" and op_path in record.created_files:
                    record.created_files.remove(op_path)
        if command.name == "web.fetch":
            # A vendor write changed a repository file exactly as
            # repo.write_file would have, so the session's record of what this
            # run touched must show it; otherwise a run could end claiming it
            # changed nothing while a fetched file sits in the tree.
            #
            # It must also record files it created. A download is a scratch
            # copy, and the model routinely removes it before finishing. When
            # the path never reached ``created_files`` the destructive gate
            # treated that cleanup as deleting the user's source: it asked for
            # a confirmation an unattended run cannot give, and one benchmark
            # run burned 37 refused calls inventing confirmation fields.
            saved = result.data.get("path")
            if result.data.get("changed") is True and isinstance(saved, str):
                if saved not in record.changed_files:
                    record.changed_files.append(saved)
                if (
                    result.data.get("previous_sha256") is None
                    and saved not in record.created_files
                ):
                    record.created_files.append(saved)
            return
        if command.name == "tests.run":
            record.checks.append(
                {
                    "correlation_id": command.correlation_id,
                    "ok": not result.data.get("timed_out", False)
                    and result.data.get("exit_code") == 0,
                    "argv": result.data.get("argv", []),
                    "exit_code": result.data.get("exit_code"),
                    "timed_out": result.data.get("timed_out", False),
                    "suite": result.data.get("suite"),
                    "split": result.data.get("split"),
                    "part": result.data.get("part"),
                }
            )
            return
        if command.name != "repo.edit_file":
            return
        path = result.data.get("path")
        if (
            result.data.get("changed") is True
            and isinstance(path, str)
            and path not in record.changed_files
        ):
            record.changed_files.append(path)

    def _validate_idempotent_replay(
        self, command: CoreCommand, result: CoreResult
    ) -> None:
        super()._validate_idempotent_replay(command, result)
        if command.name not in {"repo.command", "repo.apply_patch"} or result.data.get("dry_run") is True:
            return
        post_state = result.data.get("post_state")
        if not isinstance(post_state, dict):
            raise InvalidCommand("stored repo.command result has no post_state")
        for relative, expected in post_state.items():
            if not isinstance(relative, str) or (
                expected is not None and not isinstance(expected, str)
            ):
                raise InvalidCommand("stored repo.command post_state is invalid")
            path = self.safe_path(relative)
            current = self._file_sha256(path) if path.is_file() else None
            if current != expected:
                raise InvalidCommand(
                    "stored repo.command result no longer matches repository state; "
                    "use a new idempotency key"
                )
        post_directories = result.data.get("post_directories", {})
        if not isinstance(post_directories, dict):
            raise InvalidCommand("stored repo.command post_directories is invalid")
        for relative, expected_exists in post_directories.items():
            if not isinstance(relative, str) or not isinstance(expected_exists, bool):
                raise InvalidCommand("stored repo.command post_directories is invalid")
            path = self.safe_path(relative, for_write=True)
            if path.is_dir() != expected_exists:
                raise InvalidCommand(
                    "stored repo.command result no longer matches repository state; "
                    "use a new idempotency key"
                )

    # -- repo.edit_file ---------------------------------------------------

    def _prepare_edit(
        self, arguments: Dict[str, Any]
    ) -> tuple[str, List[EditOp], Path, bool, int]:
        """Validate the call and turn it into edit operations.

        This runs in preflight too, before an idempotency intent is reserved, so
        it may only look at the arguments: a deterministically bad call must be
        refused before anything durable names it.
        """
        relative = self._required(arguments, "path", str)
        batch = arguments.get("edits") is not None
        pair = "old_string" in arguments or "new_string" in arguments
        if batch and pair:
            raise InvalidCommand(
                "pass edits=[{old,new}] or old_string/new_string, not both"
            )
        if not batch and not pair:
            raise InvalidCommand(
                "pass edits=[{old,new}] for several replacements or "
                "old_string/new_string for one"
            )
        expected = 1
        if batch:
            ops = self._parse_edits(arguments.get("edits"))
        else:
            old_string = self._required(arguments, "old_string", str)
            new_string = self._required(arguments, "new_string", str)
            if not old_string:
                raise InvalidCommand("old_string must not be empty")
            if old_string == new_string:
                raise InvalidCommand("old_string and new_string are identical")
            requested = arguments.get("expected_occurrences", 1)
            if isinstance(requested, bool) or not isinstance(requested, (int, float)):
                raise InvalidCommand("expected_occurrences must be number")
            expected = int(requested)
            if expected < 1 or expected > self.MAX_EDIT_OCCURRENCES:
                raise InvalidCommand(
                    "expected_occurrences must be between 1 and "
                    f"{self.MAX_EDIT_OCCURRENCES}"
                )
            ops = [EditOp(old_string, new_string)]
        for op in ops:
            if contains_credential(op.new) and not self._secret_literal_allowed(
                arguments
            ):
                raise CoreError(
                    "edit blocked by credential scanner; pass allow_secret_literal "
                    "to author a fixture or documentation example on purpose"
                )
        path = self.safe_path(relative, for_write=True)
        return relative, ops, path, batch, expected

    def _parse_edits(self, value: Any) -> List[EditOp]:
        """The ``edits`` array as operations, or a refusal naming the bad entry."""
        if not isinstance(value, list) or not value:
            raise InvalidCommand("edits must be a non-empty array")
        if len(value) > self.MAX_EDIT_BATCH:
            raise InvalidCommand(
                f"edits accepts at most {self.MAX_EDIT_BATCH} replacements per call"
            )
        ops: List[EditOp] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise InvalidCommand(f"edits[{index}] must be an object")
            unknown = set(item).difference({"old", "new", "occurrence"})
            if unknown:
                raise InvalidCommand(
                    f"edits[{index}] has unknown keys: {sorted(unknown)}"
                )
            old = item.get("old")
            new = item.get("new")
            if not isinstance(old, str):
                raise InvalidCommand(f"edits[{index}].old must be a string")
            if not isinstance(new, str):
                raise InvalidCommand(f"edits[{index}].new must be a string")
            occurrence: str | int = UNIQUE
            if "occurrence" in item:
                raw = item["occurrence"]
                if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                    raise InvalidCommand(
                        f"edits[{index}].occurrence must be {UNIQUE!r}, {ALL!r} "
                        "or an occurrence number"
                    )
                if isinstance(raw, str):
                    if raw not in {UNIQUE, ALL}:
                        raise InvalidCommand(
                            f"edits[{index}].occurrence must be {UNIQUE!r}, "
                            f"{ALL!r} or an occurrence number"
                        )
                    occurrence = raw
                elif raw < 1:
                    raise InvalidCommand(
                        f"edits[{index}].occurrence must be 1 or greater"
                    )
                else:
                    occurrence = raw
            ops.append(EditOp(old, new, occurrence))
        return ops

    def _edit_file(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        relative, ops, path, batch, expected = self._prepare_edit(arguments)
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size > self.MAX_FILE_BYTES:
            raise CoreError(f"file is larger than {self.MAX_FILE_BYTES} bytes")
        raw = path.read_bytes()
        previous_digest = hashlib.sha256(raw).hexdigest()
        # An edit is written against a file the caller read earlier. Between the
        # read and the write, a check, a commit, a formatter or a second client
        # may have rewritten it -- and an exact-string match can still succeed on
        # the new content, quietly applying the edit to a file the caller never
        # saw. Naming the digest that was read turns that into a refusal.
        expected_digest = arguments.get("expected_sha256")
        if expected_digest is not None:
            if not isinstance(expected_digest, str) or not _SHA256.fullmatch(
                expected_digest
            ):
                raise InvalidCommand("expected_sha256 must be a sha256 hex digest")
            if expected_digest.lower() != previous_digest:
                raise InvalidCommand(
                    "file changed since it was read: expected "
                    f"{expected_digest.lower()} but found {previous_digest}"
                )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.edit_file supports UTF-8 text only") from exc
        if batch:
            updated, failures = apply_edits(text, ops)
            if failures:
                return self._edit_failure_payload(relative, failures, previous_digest)
            replacements = _replaced_occurrences(text, ops)
            # No-op entries in a mixed batch are skipped by the engine.
            edits_applied = sum(1 for op in ops if op.old != op.new)
            line_ending_adapted = False
        else:
            replacement_old, replacement_new, found, line_ending_adapted = (
                self._legacy_match(text, ops[0], expected)
            )
            updated, failures = apply_edits(
                text,
                [
                    EditOp(
                        replacement_old,
                        replacement_new,
                        UNIQUE if expected == 1 else ALL,
                    )
                ],
            )
            if failures:
                # The count above already proved the anchor is in this text, so
                # this can only mean the anchor stopped being unique between the
                # two lookups. Report it as the refusal it is.
                raise InvalidCommand(
                    f"edit could not be applied: {failures[0].hint}"
                )
            replacements = found
            edits_applied = 1
        # Reuse the audited atomic writer so permissions, fsync, temporary file
        # cleanup and the size ceiling behave exactly as for repo.write_file.
        result = self._write_file(
            {
                "path": relative,
                "content": updated,
                # The edit already made this decision; the write must not make
                # it again and refuse content the caller was allowed to author.
                "allow_secret_literal": self._secret_literal_allowed(arguments),
            },
            deadline_seconds,
        )
        result["replacements"] = replacements
        result["edits_applied"] = edits_applied
        result["line_ending_adapted"] = line_ending_adapted
        result["previous_sha256"] = previous_digest
        result["diff"] = _edit_diff(relative, text, updated)
        if not batch:
            result["_evidence"] = [
                EvidenceRecord(
                    kind="file_edit",
                    summary=f"Replaced {found} occurrence(s) in {relative}",
                    artifact_sha256=result.get("sha256"),
                    metadata={
                        "path": relative,
                        "replacements": found,
                        "changed": bool(result.get("changed")),
                    },
                )
            ]
        return result

    def _legacy_match(
        self, text: str, op: EditOp, expected: int
    ) -> tuple[str, str, int, bool]:
        """The one-replacement form's anchor, counted and adapted as before.

        ``old_string``/``new_string`` keep the contract they always had: an
        explicit occurrence count, and a CRLF anchor that still finds its line
        when the caller quoted what ``repo.read_lines`` returned.
        """
        replacement_old = op.old
        replacement_new = op.new
        found = text.count(replacement_old)
        line_ending_adapted = False
        if found == 0:
            if "\r\n" in text and "\r\n" not in op.old and "\n" in op.old:
                candidate_old = op.old.replace("\n", "\r\n")
                candidate_new = op.new.replace("\n", "\r\n")
                candidate_found = text.count(candidate_old)
                if candidate_found:
                    replacement_old = candidate_old
                    replacement_new = candidate_new
                    found = candidate_found
                    line_ending_adapted = True
            elif "\r\n" not in text and "\r\n" in op.old:
                candidate_old = op.old.replace("\r\n", "\n")
                candidate_new = op.new.replace("\r\n", "\n")
                candidate_found = text.count(candidate_old)
                if candidate_found:
                    replacement_old = candidate_old
                    replacement_new = candidate_new
                    found = candidate_found
                    line_ending_adapted = True
        if found != expected:
            raise InvalidCommand(
                f"expected {expected} occurrence(s) of old_string "
                f"but found {found}"
            )
        return replacement_old, replacement_new, found, line_ending_adapted

    @staticmethod
    def _edit_failure_payload(
        relative: str, failures: List[EditFailure], previous_digest: str
    ) -> Dict[str, Any]:
        """A refused batch: no side effect, and the reason for every edit.

        The failures travel in the result rather than as an exception because
        they are the repair instructions -- a model that reads them can fix the
        anchor in the next call instead of guessing again.
        """
        return {
            "path": relative,
            "changed": False,
            "edits_applied": 0,
            "error_code": "edit_failed",
            "message": "; ".join(failure.hint for failure in failures[:3]),
            "failures": [failure.to_dict() for failure in failures],
            "previous_sha256": previous_digest,
            # Nothing was written, so the file's digest is also its post-state:
            # a replay of this refusal still validates against unchanged content.
            "sha256": previous_digest,
            "_evidence": [],
        }

    # -- plan.update ------------------------------------------------------

    @staticmethod
    def _prepare_plan_update(arguments: Dict[str, Any]) -> List[Dict[str, str]]:
        """Validate the model's plan list.

        The whole list arrives every time: a plan that had to be patched item by
        item would need a diff of a document that is already tiny, and a partial
        update cannot express "I dropped this".
        """
        raw = arguments.get("items")
        if not isinstance(raw, list):
            raise InvalidCommand("items must be an array")
        if len(raw) > ExtendedCoreRuntime.MAX_PLAN_ITEMS:
            raise InvalidCommand(
                "items accepts at most "
                f"{ExtendedCoreRuntime.MAX_PLAN_ITEMS} entries"
            )
        items: List[Dict[str, str]] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise InvalidCommand(f"items[{index}] must be an object")
            unknown = set(entry).difference({"text", "status"})
            if unknown:
                raise InvalidCommand(
                    f"items[{index}] has unknown keys: {sorted(unknown)}"
                )
            label = entry.get("text")
            status = entry.get("status")
            if not isinstance(label, str) or not label.strip():
                raise InvalidCommand(f"items[{index}].text must be a non-empty string")
            if len(label) > ExtendedCoreRuntime.MAX_PLAN_ITEM_CHARS:
                raise InvalidCommand(
                    f"items[{index}].text must be at most "
                    f"{ExtendedCoreRuntime.MAX_PLAN_ITEM_CHARS} characters"
                )
            if status not in {"todo", "doing", "done"}:
                raise InvalidCommand(
                    f"items[{index}].status must be todo, doing or done"
                )
            items.append({"text": label.strip(), "status": str(status)})
        return items

    def _plan_update(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        items = self._prepare_plan_update(arguments)
        return {
            "items": len(items),
            "done": sum(1 for item in items if item["status"] == "done"),
        }

    # -- repo.read_lines --------------------------------------------------

    def _read_lines(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        path = self.safe_path(self._required(arguments, "path", str))
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size > self.MAX_FILE_BYTES:
            raise CoreError(f"file is larger than {self.MAX_FILE_BYTES} bytes")
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.read_lines supports UTF-8 text only") from exc
        start = self._bounded_number(arguments, "start", 1, 1)
        count = self._bounded_number(
            arguments, "count", self.MAX_READ_LINES, 1, self.MAX_READ_LINES
        )
        lines = text.splitlines()
        selected = lines[start - 1 : start - 1 + count]
        # The digest of the range as returned, not of the whole file: a caller
        # that compares two reads of the same window is asking about the window,
        # and a file edited outside the range has not changed the answer.
        returned = "\n".join(selected)
        return {
            "path": path.relative_to(self.repository).as_posix(),
            "start": start,
            "count": len(selected),
            "total_lines": len(lines),
            "has_more": (start - 1 + len(selected)) < len(lines),
            "content_sha256": hashlib.sha256(returned.encode("utf-8")).hexdigest(),
            # Byte-faithful, for the same reason as repo.read_file: a line
            # rewritten by pattern redaction cannot be used as an edit anchor.
            "lines": [str(redact_content(item)) for item in selected],
            "secret_like": any(contains_credential(item) for item in selected),
        }

    @staticmethod
    def _bounded_number(
        arguments: Dict[str, Any],
        name: str,
        default: int,
        minimum: int,
        maximum: Optional[int] = None,
    ) -> int:
        raw = arguments.get(name, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidCommand(f"{name} must be number")
        value = int(raw)
        if value < minimum:
            raise InvalidCommand(f"{name} must be {minimum} or greater")
        if maximum is not None:
            value = min(value, maximum)
        return value

    # -- git.log ----------------------------------------------------------

    def _git_log(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        limit = self._bounded_number(
            arguments, "limit", 20, 1, self.MAX_LOG_ENTRIES
        )
        result = self._git(
            [
                "log",
                f"--max-count={limit}",
                "--no-decorate",
                "--date=iso-strict",
                "--pretty=format:%h%x09%ad%x09%an%x09%s",
            ],
            deadline_seconds,
        )
        result["limit"] = limit
        result["_evidence"] = [self._git_evidence("git_log", result)]
        return result

    # -- git.diff ---------------------------------------------------------

    def _git_diff_paths(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise InvalidCommand("staged must be boolean")
        raw_paths = arguments.get("paths", [])
        if not isinstance(raw_paths, list):
            raise InvalidCommand("paths must be array")
        if len(raw_paths) > self.MAX_DIFF_PATHS:
            raise InvalidCommand(
                f"paths must contain at most {self.MAX_DIFF_PATHS} entries"
            )
        relatives: List[str] = []
        for item in raw_paths:
            if not isinstance(item, str):
                raise InvalidCommand("paths items must be string")
            # Same confinement as every other path-taking tool: traversal,
            # absolute paths, links and metadata directories are rejected before
            # Git is invoked.
            resolved = self.safe_path(item)
            relative = resolved.relative_to(self.repository).as_posix()
            if relative not in relatives:
                relatives.append(relative)
        command = ["diff", "--no-ext-diff"]
        if staged:
            command.append("--cached")
        if relatives:
            command.append("--")
            command.extend(relatives)
        result = self._git(command, deadline_seconds)
        result["paths"] = list(relatives)
        result["_evidence"] = [self._git_evidence("git_diff", result)]
        return result

    # -- repo.list_files --------------------------------------------------

    def _list_files(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        pattern = arguments.get("pattern", "**/*")
        raw_pattern = Path(pattern) if isinstance(pattern, str) else None
        if (
            raw_pattern is None
            or not pattern
            or len(pattern) > 1000
            or "\x00" in pattern
            or raw_pattern.is_absolute()
            or raw_pattern.drive
            or ".." in raw_pattern.parts
        ):
            raise InvalidCommand(
                "pattern must be repository-relative: "
                + _path_label(str(raw_pattern))
            )
        # The unqualified default is the hot path for hosted agents. Python's
        # ``Path.glob('**/*')`` descends into node_modules/.venv *before* our
        # ignore predicate sees a candidate, so a normal JS repository can hit
        # the hosted 60s tool timeout just to list files. Git already owns the
        # repository index and ignore rules; use it for the broad listing and
        # keep globbing only for an explicitly bounded pattern.
        candidates: Iterable[str | Path]
        if pattern == "**/*":
            result = self._git(
                ["ls-files", "--cached", "--others", "--exclude-standard"],
                deadline_seconds,
            )
            candidates = [
                line for line in result.get("stdout", "").splitlines() if line
            ]
        else:
            try:
                candidates = self.repository.glob(pattern)
            except (OSError, ValueError) as exc:
                raise InvalidCommand(f"invalid glob pattern: {exc}") from exc

        items: List[str] = []
        truncated = False
        for candidate in candidates:
            try:
                if isinstance(candidate, str):
                    lexical = candidate.replace("\\", "/")
                else:
                    if not candidate.is_file():
                        continue
                    lexical = candidate.relative_to(self.repository).as_posix()
                safe = self.safe_path(lexical)
                relative = safe.relative_to(self.repository)
            except (InvalidPath, ValueError):
                continue
            if relative.parts and relative.parts[0].lower() in {".git", ".karox"}:
                continue
            posix = relative.as_posix()
            # Filtering happens before the ceiling is applied, so dependency
            # directories cannot crowd real source out of a truncated result.
            if is_ignored_path(posix):
                continue
            if len(items) >= self.MAX_LISTED_FILES:
                truncated = True
                break
            items.append(posix)
        return {
            "files": sorted(items),
            "truncated": truncated,
        }

    # -- web.fetch ---------------------------------------------------------

    def _prepare_fetch(self, arguments: Dict[str, Any]) -> tuple[str, str]:
        url, host = code_source_url(self._required(arguments, "url", str))
        destination = arguments.get("path")
        if destination is not None:
            if not isinstance(destination, str) or not destination.strip():
                raise InvalidCommand("path must be a non-empty repository-relative file")
            # Validating the destination here keeps the confinement refusal in
            # the preflight, before any part of the network read has happened.
            self.safe_path(destination, for_write=True)
        self._bounded_number(
            arguments, "max_chars", self.MAX_FETCH_CHARS, 1, self.MAX_FETCH_CHARS
        )
        return url, host

    def _read_fetched_body(self, response: Any, deadline: float) -> tuple[bytes, bool]:
        """Read at most the ceiling, and say whether the source was longer.

        ``read1`` performs at most one underlying read, so a source that
        trickles bytes cannot restart an unbounded ``read(n)``. The deadline is
        re-checked on every chunk, because a slow-drip response would otherwise
        outlast the per-read socket timeout for as many chunks as it cared to
        send.
        """

        chunks: List[bytes] = []
        total = 0
        truncated = False
        while True:
            if time.monotonic() >= deadline:
                raise CoreError(
                    "the code source was still sending after the time allowed for "
                    "one source file; nothing was used"
                )
            chunk = response.read1(FETCH_CHUNK_BYTES)
            if not chunk:
                break
            remaining = self.MAX_FETCH_BYTES - total
            if len(chunk) > remaining:
                # The ceiling is a cut, not a discard: a source one byte over
                # the limit must still return the part that fits rather than
                # nothing at all.
                chunks.append(chunk[:remaining])
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), truncated

    def _fetch_source(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        url, host = self._prepare_fetch(arguments)
        destination = arguments.get("path")
        max_chars = self._bounded_number(
            arguments, "max_chars", self.MAX_FETCH_CHARS, 1, self.MAX_FETCH_CHARS
        )
        timeout = min(self.FETCH_TIMEOUT_SECONDS, max(1.0, float(deadline_seconds)))
        deadline = time.monotonic() + timeout
        request = Request(
            url,
            headers={
                "User-Agent": "KaroX-code-fetch",
                "Accept": "text/*, application/javascript, application/json",
                # An identity encoding keeps the byte count meaningful, so the
                # ceiling is a real ceiling rather than a compressed guess.
                "Accept-Encoding": "identity",
            },
            method="GET",
        )
        try:
            with build_opener(_CodeSourceRedirects()).open(
                request, timeout=timeout
            ) as response:
                final = str(getattr(response, "geturl", lambda: url)() or url)
                # Checked again after the fact: the redirect hook is one path to
                # the body and this is the other.
                final_url, final_host = code_source_url(final)
                status = int(getattr(response, "status", 200) or 200)
                content_type = str(response.headers.get_content_type() or "")
                declared = response.headers.get("Content-Length")
                size_hint = int(declared) if (declared or "").isdigit() else None
                if (
                    destination is not None
                    and size_hint is not None
                    and size_hint > self.MAX_FETCH_BYTES
                ):
                    # Refused before a byte is read: a library announced larger
                    # than the saving ceiling cannot be saved whole, and a
                    # half-saved library in the repository is worse than none.
                    raise CoreError(
                        f"the source is {size_hint} bytes, larger than the "
                        f"{self.MAX_FETCH_BYTES}-byte saving ceiling; fetch a smaller "
                        "file, or leave path out to read it in the answer where it "
                        "is cut at the in-answer limit"
                    )
                body, truncated = self._read_fetched_body(response, deadline)
        except HTTPError as exc:
            raise CoreError(
                f"the code source answered HTTP {exc.code} for {redact(url)}"
            ) from exc
        except (URLError, OSError, TimeoutError) as exc:
            # The transport error may quote a proxy URL or a signed redirect
            # query, so it is deliberately not echoed into the conversation.
            raise CoreError(
                f"reading {redact(url)} failed (network, TLS, timeout or local I/O); "
                "check connectivity and the exact source path"
            ) from exc
        if not _is_code_source_type(content_type):
            raise CoreError(
                f"{redact(final_url)} answered {content_type or '(no type)'}, "
                "which is not a text source file"
            )
        text = body.decode("utf-8", errors="replace")
        digest = hashlib.sha256(body).hexdigest()
        if destination is not None and truncated:
            # A ceiling that was reached while saving is a refusal, not a note:
            # a half-saved library in the repository looks exactly like a
            # complete one to every later read.
            raise CoreError(
                f"the source is longer than the {self.MAX_FETCH_BYTES}-byte saving "
                "ceiling; nothing was written, because a half-saved library is worse "
                "than none. Fetch a smaller file, or leave path out and read it in "
                "the answer"
            )
        payload: Dict[str, Any] = {
            "url": final_url,
            "host": final_host,
            "status": status,
            "content_type": content_type,
            "bytes": len(body),
            "sha256": digest,
            "truncated": truncated,
            "secret_like": contains_credential(text),
        }
        evidence = [
            EvidenceRecord(
                kind="code_fetch",
                summary=f"Read {host}{urlsplit(final_url).path}",
                artifact_sha256=digest,
                metadata={
                    "url": str(redact(final_url)),
                    "host": host,
                    "status": status,
                    "bytes": len(body),
                    "truncated": truncated,
                },
            )
        ]
        if destination is not None:
            written = self._write_file(
                {
                    "path": destination,
                    "content": text,
                    "allow_secret_literal": self._secret_literal_allowed(arguments),
                },
                deadline_seconds,
            )
            payload.update(
                {
                    "path": written.get("path"),
                    "changed": written.get("changed"),
                    "content_sha256": written.get("sha256"),
                    # None means this fetch created the file. The session needs
                    # that fact to allow the model to remove its own scratch
                    # copy without a confirmation nobody can answer.
                    "previous_sha256": written.get("previous_sha256"),
                }
            )
            payload["_evidence"] = [*evidence, *written.get("_evidence", [])]
            return payload
        returned = str(redact_content(text))
        shortened = len(returned) > max_chars
        if shortened:
            returned = returned[:max_chars]
        payload["content"] = returned
        payload["content_sha256"] = hashlib.sha256(returned.encode("utf-8")).hexdigest()
        payload["shortened"] = shortened
        if shortened or truncated:
            payload["detail"] = (
                f"only the first {max_chars} of {len(text)} characters are included; "
                "pass path to save the whole source into the repository and read it "
                "there with repo.read_lines or repo.search"
            )
        payload["_evidence"] = evidence
        return payload
