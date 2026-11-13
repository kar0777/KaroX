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

from .core_skill_tools import CoreSkillToolsMixin
from .core_job_tools import CoreJobToolsMixin
from .core_edit_tools import CoreEditToolsMixin
from .core_read_tools import (
    CoreReadToolsMixin,
    IGNORED_DIRECTORY_NAMES as IGNORED_DIRECTORY_NAMES,
    ROOT_IGNORED_DIRECTORY_NAMES as ROOT_IGNORED_DIRECTORY_NAMES,
    _IGNORED_DIRECTORY_PREFIXES as _IGNORED_DIRECTORY_PREFIXES,
    _IGNORED_DIRECTORY_SUFFIXES as _IGNORED_DIRECTORY_SUFFIXES,
    is_ignored_path as is_ignored_path,
)

import hashlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import (
    CoreError,
    CoreRuntime,
    InvalidCommand,
    _path_label,
)
from .models import CoreCommand, CoreResult, EvidenceRecord
from .security import contains_credential, redact, redact_content
from .sessions import MutationLease
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
#: Files visual.check can render. A standalone SVG is a picture the browser
#: draws on its own, so it is judged exactly like a page.
OUTPUT_ELISION_TRIGGER_CHARS = 8_000
OUTPUT_ELISION_HEAD_CHARS = 2_000
OUTPUT_ELISION_TAIL_CHARS = 3_000
OUTPUT_ELISION_TOOLS = frozenset({"checks.run", "repo.command", "dev.command"})
_OUTPUT_TEXT_FIELDS = ("stdout", "stderr")



#: Vendored copies of this project under ``tools/`` are named after the version
#: they adapted, so the rule is a prefix rather than a name.





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


def repository_file_segments(url: str) -> tuple[str, ...]:
    """The path inside a Git repository that a code-source URL names, if any.

    ``raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}`` and
    ``cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}`` each point at one file
    of one Git repository. A ref can itself hold slashes, so the result keeps
    everything after owner, repo and the first ref segment and callers match
    its suffixes. Package CDNs (unpkg, npm builds, cdnjs, esm.sh) serve
    published libraries, not a project's working tree, and return ``()``.
    """

    try:
        parts = urlsplit(url)
    except ValueError:
        return ()
    host = (parts.hostname or "").lower()
    segments = [unquote(item) for item in parts.path.split("/") if item]
    if host == "githubusercontent.com" or host.endswith(".githubusercontent.com"):
        return tuple(segments[3:])
    if (host == "jsdelivr.net" or host.endswith(".jsdelivr.net")) and segments[:1] == ["gh"]:
        return tuple(segments[3:])
    return ()


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






class ExtendedCoreRuntime(
    CoreSkillToolsMixin,
    CoreJobToolsMixin,
    CoreEditToolsMixin,
    CoreReadToolsMixin,
    CoreRuntime,
):
    """Core runtime with editing, windowed reads and history inspection."""

    # The base small-tree search fast path reads these attributes dynamically,
    # so dependency/build filtering stays owned by this Extended Core module
    # without creating a core.py -> core_tools.py import cycle.
    SEARCH_IGNORED_DIRECTORY_NAMES = IGNORED_DIRECTORY_NAMES
    SEARCH_ROOT_IGNORED_DIRECTORY_NAMES = ROOT_IGNORED_DIRECTORY_NAMES
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
                "Read the current Git diff, optionally limited to given paths, "
                "or the whole change since the session's start revision when "
                "complete=true. With rev, read a past change instead: what one "
                "commit did, or an A..B range (e.g. v8.2.1..v8.3.0)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "rev": {"type": "string"},
                    "complete": {
                        "type": "boolean",
                        "description": (
                            "Read the whole change since the session's start "
                            "revision: staged and unstaged tracked edits plus "
                            "every new untracked file. Cannot be combined with "
                            "staged or with paths."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        )

    # -- repo.inspect -------------------------------------------------------


    # -- skill.list / skill.load / skill.read ------------------------------





    # -- visual.check ------------------------------------------------------


    # -- disk maintenance --------------------------------------------------



    # -- hot developer worker ---------------------------------------------






    # -- background jobs (hosted clients) ----------------------------------

















        # The handler builds and validates one complete WorkspaceTransaction
        # before its first filesystem mutation. Running a separate dry-run here
        # created a second transaction, widened the preflight/commit race, and
        # made an immediate idempotent replay re-apply patch context to the
        # already-updated tree before the stored result could be returned.


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




    # -- repo.edit_file ---------------------------------------------------






    # -- plan.update ------------------------------------------------------



    # -- repo.read_lines --------------------------------------------------



    # -- git.log ----------------------------------------------------------


    # -- git.diff ---------------------------------------------------------


    # -- repo.list_files --------------------------------------------------


    # -- web.fetch ---------------------------------------------------------

    def _prepare_fetch(self, arguments: Dict[str, Any]) -> tuple[str, str]:
        url, host = code_source_url(self._required(arguments, "url", str))
        local = self._local_copy_of(url)
        if local is not None:
            # The prompt already says to solve from the repository in front of
            # the model, yet measured runs still read the upstream copy of the
            # very module they were fixing (a units-system file from the
            # project's own GitHub master). Isolated runs only lost a step to
            # DNS; with a network the run would copy a fix instead of making
            # one. Refused before any network read.
            raise InvalidCommand(
                f"this URL is another copy of {local}, which the repository "
                f"already has; solve from the repository's own code (read {local}). "
                "web_fetch is for sources and documentation the repository lacks"
            )
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

    def _local_copy_of(self, url: str) -> Optional[str]:
        """The repository file ``url`` is another copy of, or None.

        Matched on the trailing segments of the in-repository path, two or
        more of them, longest first: ``sympy/physics/units/unitsystem.py`` or
        ``requests/utils.py`` match a local file of that path, while a bare
        ``utils.py`` never does.
        """

        segments = repository_file_segments(url)
        for start in range(0, len(segments) - 1):
            relative = "/".join(segments[start:])
            try:
                candidate = self.safe_path(relative)
            except Exception:  # noqa: BLE001 - an unusable path is no local copy
                continue
            if candidate.is_file():
                return relative
        return None

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