"""MCP tools behind the karo-core engine loop.

The vendored engine (``karox.engine``) executes one bash command per step
through ``CoreEngineConfig.command_runner``.  This module is the narrow seam
that gives that loop the release MCP surface -- outbound stdio and Streamable
HTTP clients, schema/allow-ask-deny selections, capability policy, mutation
leases and idempotent replay -- without importing the legacy chat runtime:

* :class:`EngineMcpAdapter` exposes the session's selected tools as a typed
  Python API (``call_tool`` with typed results and progress events) plus an
  in-band ``mcp`` shell command the engine's agent runs like any other
  command.
* :func:`build_engine_command_runner` composes that command handler with the
  kernel's own command runner, so ``CoreEngineConfig(command_runner=...)`` is
  the only wiring the kernel integrator needs.

The fail-closed boundaries are applied in the same order the Core runtime
applies them for hosted MCP calls: capability policy, session binding and
revocation, stored selection identity (registry digest, schema digest,
mutation classification, permission), the mutation lease, and idempotent
replay with input-conflict and pending-outcome reconciliation.  A mutating
call that fails after reserving its idempotency intent stays pending: the
next same-key call is refused until a human reconciles the outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .mcp_client import (
    McpAccessDenied,
    McpClient,
    McpError,
    McpFailureKind,
    McpProgress,
    McpProtocolError,
    McpRegistry,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
    validate_mcp_selection_registry,
)
from .models import Capability, Origin
from .policy import CapabilityPolicy
from .security import redact
from .sessions import IdempotencyConflict, MutationLease, SessionError, SessionStore

#: Sink signature for progress events raised during a tool call. It is invoked
#: inside the transport's event loop and must stay cheap and non-blocking.
ProgressSink = Callable[["McpProgressEvent"], None]

_USAGE = (
    "usage: mcp list\n"
    "       mcp call <tool> <json-arguments> [--idempotency-key <key>]"
)
_MAX_IDEMPOTENCY_KEY = 200


@dataclass(frozen=True)
class EngineMcpTool:
    """One MCP tool the engine's agent may call, after session selection."""

    name: str
    remote_name: str
    server_id: str
    namespace: str
    description: str
    input_schema: Dict[str, Any]
    mutates: bool

    @property
    def read_only(self) -> bool:
        return not self.mutates


@dataclass(frozen=True)
class McpProgressEvent:
    """A progress notification from a remote server, attributed to a tool."""

    tool: str
    progress: float
    total: Optional[float]
    message: Optional[str]


@dataclass(frozen=True)
class EngineMcpCallResult:
    """The typed outcome of one MCP call made for the engine loop."""

    ok: bool
    tool: str
    server_id: str
    result: Dict[str, Any]
    bytes: int
    duration_ms: float
    idempotent_replay: bool
    correlation_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "server_id": self.server_id,
            "result": self.result,
            "bytes": self.bytes,
            "duration_ms": round(self.duration_ms, 3),
            "idempotent_replay": self.idempotent_replay,
            "correlation_id": self.correlation_id,
        }


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _input_digest(name: str, arguments: Dict[str, Any]) -> str:
    """The same identity Core uses: the canonical (name, arguments) digest."""

    try:
        canonical = json.dumps(
            {"name": name, "arguments": arguments},
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise McpProtocolError("MCP tool arguments must contain strict JSON values") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stored_envelope(result: EngineMcpCallResult) -> Dict[str, Any]:
    """The durable idempotency payload: everything needed to replay safely."""

    return {
        "ok": result.ok,
        "tool": result.tool,
        "server_id": result.server_id,
        "result": result.result,
        "bytes": result.bytes,
    }


def _replayed_result(envelope: Any, tool: str) -> EngineMcpCallResult:
    """Rebuild a stored outcome, refusing one that does not match the call."""

    if not isinstance(envelope, dict) or envelope.get("tool") != tool:
        raise IdempotencyConflict("stored MCP idempotent result does not match the tool")
    payload = envelope.get("result")
    if not isinstance(payload, dict) or not isinstance(envelope.get("bytes"), int):
        raise IdempotencyConflict("stored MCP idempotent result is malformed")
    return EngineMcpCallResult(
        ok=envelope.get("ok") is True,
        tool=tool,
        server_id=str(envelope.get("server_id") or ""),
        result=payload,
        bytes=int(envelope["bytes"]),
        duration_ms=0.0,
        idempotent_replay=True,
        correlation_id=uuid.uuid4().hex,
    )


class EngineMcpAdapter:
    """Selected MCP tools, callable from the engine-backed tool architecture."""

    def __init__(
        self,
        *,
        binding: McpRuntimeBinding,
        descriptors: Iterable[McpToolDescriptor],
        store: SessionStore,
        session_id: str,
        repository: Path,
        origin: Origin,
        policy: CapabilityPolicy,
        audit_path: Optional[Path] = None,
        lease_owner: Optional[str] = None,
    ) -> None:
        self._binding = binding
        # McpRuntimeBinding construction already refuses duplicate tool names,
        # so the name map is injective by the time we get here.
        self._descriptors: Dict[str, McpToolDescriptor] = {
            item.name: item for item in descriptors
        }
        self._store = store
        self._session_id = session_id
        self._repository = repository.expanduser().resolve(strict=True)
        self._origin = origin
        self._policy = policy
        self._audit_path = (
            audit_path.expanduser().resolve() if audit_path is not None else None
        )
        self._lease_owner = lease_owner or f"engine-mcp-{os.getpid()}"

    # -- construction -----------------------------------------------------

    @classmethod
    def from_session(
        cls,
        *,
        record: Any,
        repository: Path,
        store: SessionStore,
        registry: McpRegistry,
        origin: Origin,
        policy: CapabilityPolicy,
        audit_path: Optional[Path] = None,
        client: Optional[McpClient] = None,
    ) -> "EngineMcpAdapter":
        """Bind the tools a session record selected with ``allow``.

        Mirrors the CLI's stored-selection validation: every stored selection
        must still match the registry digest, every selected tool must still
        exist with the same name, schema digest and mutation classification,
        and only ``allow`` tools become callable.  Anything else fails closed
        with :class:`McpAccessDenied`.
        """

        if not isinstance(getattr(record, "mcp_servers", None), list):
            raise McpAccessDenied("stored MCP selection must be a list")
        selections: List[tuple[str, Dict[str, Any]]] = []
        seen: set[str] = set()
        for raw in record.mcp_servers:
            if not isinstance(raw, dict):
                raise McpAccessDenied("stored MCP selection must be an object")
            server_id = raw.get("server_id")
            if not isinstance(server_id, str) or not server_id:
                raise McpAccessDenied("stored MCP selection has no valid server ID")
            if server_id in seen:
                raise McpAccessDenied(
                    f"duplicate stored MCP selection: {server_id}"
                )
            seen.add(server_id)
            selections.append((server_id, raw))

        repository = repository.expanduser().resolve(strict=True)
        client = client or McpClient(registry)
        descriptors: List[McpToolDescriptor] = []
        servers: List[McpServerRecord] = []
        for server_id, raw in selections:
            server = registry.get(server_id)
            validate_mcp_selection_registry(server, raw)
            discovered = {
                item.remote_name: item
                for item in client.discover_record(server, repository)
            }
            selected_tools = raw.get("tools")
            if not isinstance(selected_tools, dict):
                raise McpAccessDenied("stored MCP tool permissions are malformed")
            for remote_name, stored in selected_tools.items():
                if not isinstance(remote_name, str) or not isinstance(stored, dict):
                    raise McpAccessDenied("stored MCP tool permission is malformed")
                descriptor = discovered.get(remote_name)
                if descriptor is None:
                    raise McpAccessDenied(
                        f"selected MCP tool disappeared: {server_id}/{remote_name}"
                    )
                if (
                    stored.get("name") != descriptor.name
                    or stored.get("schema_digest") != descriptor.schema_digest
                    or stored.get("read_only") is not descriptor.read_only
                ):
                    raise McpAccessDenied(
                        f"selected MCP tool changed: {server_id}/{remote_name}"
                    )
                permission = stored.get("permission")
                if permission not in {"allow", "ask", "deny"}:
                    raise McpAccessDenied(
                        f"selected MCP tool permission is malformed: {remote_name}"
                    )
                if permission == "allow":
                    descriptors.append(descriptor)
            servers.append(server)
        binding = McpRuntimeBinding(client, repository, descriptors, servers)
        return cls(
            binding=binding,
            descriptors=descriptors,
            store=store,
            session_id=record.session_id,
            repository=repository,
            origin=origin,
            policy=policy,
            audit_path=audit_path,
        )

    # -- typed API --------------------------------------------------------

    def tools(self) -> List[EngineMcpTool]:
        return [
            EngineMcpTool(
                name=item.name,
                remote_name=item.remote_name,
                server_id=item.server_id,
                namespace=item.namespace,
                description=item.description,
                input_schema=item.input_schema,
                mutates=item.mutates,
            )
            for item in sorted(self._descriptors.values(), key=lambda value: value.name)
        ]

    def prompt_block(self) -> str:
        """The protocol instructions the engine's system/task prompt appends."""

        lines = [
            "MCP tools are available through the `mcp` shell command:",
            "  mcp list",
            "  mcp call <tool> <json-arguments> [--idempotency-key <key>]",
            "Bound MCP tools (mutating tools require --idempotency-key; reuse",
            "the same key to retry the same mutation without repeating it):",
        ]
        for tool in self.tools():
            kind = "mutating" if tool.mutates else "read-only"
            lines.append(f"  {tool.name} ({kind}) {tool.description}")
        if not self._descriptors:
            lines.append("  (no MCP tools are selected for this session)")
        return "\n".join(lines)

    def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: Optional[float] = None,
        progress: Optional[ProgressSink] = None,
    ) -> EngineMcpCallResult:
        """Call one bound tool with the release fail-closed boundaries.

        Read-only tools run without a lease.  Mutating tools require an
        idempotency key, take the session's mutation lease, and reserve the
        durable idempotent intent before the remote call: a repeated key with
        identical input replays the stored outcome without re-executing, and a
        key reused for different input conflicts.  A failure after the
        reservation stays pending and refuses the next same-key call until the
        outcome is reconciled.
        """

        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise McpAccessDenied(f"MCP tool is not bound: {name}")
        if not isinstance(arguments, dict):
            raise McpProtocolError("MCP tool arguments must be an object")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY
        ):
            raise McpProtocolError(
                f"idempotency key must contain 1-{_MAX_IDEMPOTENCY_KEY} characters"
            )
        deadline = self._deadline(deadline_seconds)
        correlation_id = uuid.uuid4().hex
        self._policy.require(self._origin, Capability.MCP_CALL)
        record = self._load_record()
        started = time.perf_counter()
        self._audit(
            "engine-mcp.command.started",
            {
                "session_id": self._session_id,
                "origin": self._origin.key,
                "tool": name,
                "correlation_id": correlation_id,
                "mutation": descriptor.mutates,
            },
        )
        lease: Optional[MutationLease] = None
        try:
            if descriptor.mutates:
                if not idempotency_key:
                    raise McpAccessDenied(
                        f"mutating MCP tool {name} requires an idempotency key"
                    )
                lease = self._store.acquire(
                    self._session_id,
                    self._lease_owner,
                    ttl_seconds=max(30.0, min(3600.0, deadline + 10.0)),
                )
                self._store.validate_lease(lease)
                replay = self._store.begin_idempotent(
                    record, lease, idempotency_key, _input_digest(name, arguments)
                )
                if replay is not None:
                    result = _replayed_result(replay, name)
                    self._audit(
                        "engine-mcp.command.replayed",
                        {
                            "session_id": self._session_id,
                            "origin": self._origin.key,
                            "tool": name,
                            "correlation_id": correlation_id,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    return result
            try:
                data = self._binding.execute(
                    name, dict(arguments), record, progress=self._wrap_progress(name, progress)
                )
            except Exception as exc:
                self._audit(
                    "engine-mcp.command.failed",
                    {
                        "session_id": self._session_id,
                        "origin": self._origin.key,
                        "tool": name,
                        "correlation_id": correlation_id,
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            result = EngineMcpCallResult(
                ok=True,
                tool=name,
                server_id=str(data.get("server_id") or descriptor.server_id),
                result=dict(data.get("result") or {}),
                bytes=int(data.get("bytes") or 0),
                duration_ms=(time.perf_counter() - started) * 1000.0,
                idempotent_replay=False,
                correlation_id=correlation_id,
            )
            if descriptor.mutates:
                assert lease is not None and idempotency_key is not None
                try:
                    self._store.complete_idempotent(
                        record, lease, idempotency_key, _stored_envelope(result)
                    )
                except Exception as exc:
                    self._audit(
                        "engine-mcp.command.failed",
                        {
                            "session_id": self._session_id,
                            "origin": self._origin.key,
                            "tool": name,
                            "correlation_id": correlation_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    raise
            self._audit(
                "engine-mcp.command.completed",
                {
                    "session_id": self._session_id,
                    "origin": self._origin.key,
                    "tool": name,
                    "correlation_id": correlation_id,
                    "duration_ms": round(result.duration_ms, 3),
                    "bytes": result.bytes,
                },
            )
            return result
        finally:
            if lease is not None:
                self._store.release(lease)

    # -- in-band command protocol ----------------------------------------

    def handle_raw(
        self, command: str, timeout: int = 300
    ) -> Optional[Dict[str, Any]]:
        """Route one raw shell command if it is an ``mcp`` invocation.

        Returns the engine observation dict (``output``/``returncode``) for an
        ``mcp`` command, or ``None`` when the command belongs to the fallback
        runner.  Usage mistakes are usage observations (exit 2), never
        exceptions: the engine loop treats a thrown runner as an environment
        failure, while a bad tool call is just an observation the model can
        correct.
        """

        if not isinstance(command, str) or not command.strip():
            return None
        if command.strip().split(None, 1)[0] != "mcp":
            return None
        try:
            argv = shlex.split(command.strip())
        except ValueError as exc:
            return self._usage(f"unparsable mcp command: {exc}")
        return self.handle_command(argv, timeout)

    def handle_command(
        self, argv: Sequence[str], timeout: int = 300
    ) -> Dict[str, Any]:
        argv = list(argv)
        if not argv or argv[0] != "mcp":
            return self._usage("not an mcp command")
        if len(argv) == 1:
            return self._usage("missing mcp subcommand")
        if argv[1] == "list" and len(argv) == 2:
            return self._observation(0, self._list_output())
        if argv[1] == "call":
            return self._call_from_argv(argv[2:], timeout)
        return self._usage(f"unknown mcp subcommand: {argv[1]!r}")

    def _call_from_argv(self, rest: Sequence[str], timeout: int) -> Dict[str, Any]:
        if len(rest) < 2:
            return self._usage("mcp call requires a tool name and JSON arguments")
        name = rest[0]
        raw_arguments = rest[1]
        idempotency_key: Optional[str] = None
        index = 2
        while index < len(rest):
            if rest[index] == "--idempotency-key":
                if index + 1 >= len(rest):
                    return self._usage("--idempotency-key requires a value")
                idempotency_key = rest[index + 1]
                index += 2
                continue
            return self._usage(f"unexpected token: {rest[index]!r}")
        try:
            arguments = json.loads(raw_arguments, parse_constant=_reject_constant)
        except ValueError as exc:
            return self._usage(f"arguments are not valid JSON: {exc}")
        if not isinstance(arguments, dict):
            return self._usage("arguments must be a JSON object")
        try:
            result = self.call_tool(
                name,
                arguments,
                idempotency_key=idempotency_key,
                deadline_seconds=self._deadline(timeout),
            )
        except McpError as exc:
            kind = McpFailureKind(exc.kind).value
            return self._observation(1, f"mcp error ({kind}): {redact(str(exc))}")
        except IdempotencyConflict as exc:
            return self._observation(1, f"mcp error (conflict): {exc}")
        except Exception as exc:  # session/policy failures stay typed, not crashes
            return self._observation(
                1, f"mcp error (session): {redact(f'{type(exc).__name__}: {exc}')}"
            )
        replay = " (idempotent replay)" if result.idempotent_replay else ""
        header = (
            f"mcp: {result.tool} ok{replay} "
            f"({result.bytes} bytes, {result.duration_ms:.0f} ms)"
        )
        payload = json.dumps(
            result.result,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        return self._observation(0, f"{header}\n{payload}")

    def _list_output(self) -> str:
        tools = self.tools()
        if not tools:
            return "mcp: no MCP tools are selected for this session"
        lines = []
        for tool in tools:
            kind = "mutating" if tool.mutates else "read-only"
            description = tool.description.splitlines()[0] if tool.description else ""
            lines.append(f"{tool.name:<48} {kind:<10} {description}")
        return "\n".join(lines)

    # -- internals --------------------------------------------------------

    def _wrap_progress(
        self, name: str, progress: Optional[ProgressSink]
    ) -> Optional[Callable[[McpProgress], None]]:
        if progress is None:
            return None

        def sink(event: McpProgress) -> None:
            progress(
                McpProgressEvent(
                    tool=name,
                    progress=event.progress,
                    total=event.total,
                    message=event.message,
                )
            )

        return sink

    def _deadline(self, value: Optional[float]) -> float:
        if value is None:
            value = 300.0
        return max(0.1, min(3600.0, float(value)))

    def _load_record(self) -> Any:
        record = self._store.load(self._session_id)
        self._store.validate_repository(record, self._repository)
        if record.revoked:
            raise SessionError("session access has been revoked")
        if record.access_profile != self._policy.profile.value:
            raise SessionError("session and runtime access profiles differ")
        return record

    def _observation(self, returncode: int, output: str) -> Dict[str, Any]:
        return {"output": output, "returncode": returncode}

    def _usage(self, detail: str) -> Dict[str, Any]:
        return {"output": f"{_USAGE}\n{detail}", "returncode": 2}

    def _audit(self, event: str, payload: Dict[str, Any]) -> None:
        if self._audit_path is None:
            return
        try:
            row = {"event": event, "ts": round(time.time(), 3), **payload}
            with self._audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    + "\n"
                )
        except Exception:
            # Observability must never be able to block or fail a tool call.
            return


def build_engine_command_runner(
    adapter: EngineMcpAdapter,
    fallback: Callable[[str, Path, int], Dict[str, Any]],
) -> Callable[[str, Path, int], Dict[str, Any]]:
    """Compose the in-band ``mcp`` command with the kernel's command runner.

    The engine loop asks one runner to execute every bash command.  This
    builder routes ``mcp ...`` invocations to the adapter and everything else
    to ``fallback`` -- the kernel integrator's policy-aware shell runner::

        adapter = EngineMcpAdapter.from_session(...)
        runner = build_engine_command_runner(adapter, shell_runner)
        CoreEngineConfig(..., command_runner=runner)
    """

    def runner(command: str, cwd: Path, timeout: int) -> Dict[str, Any]:
        observation = adapter.handle_raw(command, timeout if timeout else 300)
        if observation is not None:
            return observation
        return fallback(command, cwd, timeout)

    return runner
