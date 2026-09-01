"""Wire-level acceptance for a live saved KaroX MCP bridge.

The probe resolves the saved bridge credential from the OS keyring and never
prints it.  Every cycle uses a fresh Streamable HTTP client and verifies the
failure-prone sequence: initialize, tools/list, immediate diagnostics call,
then additional diagnostics calls.  Multiple clients can run concurrently.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from karox.bridge import BridgeCredentialStore  # noqa: E402
from karox.mcp_client import streamable_http_transport  # noqa: E402
from karox.proxy_server import wire_tool_name  # noqa: E402
from karox.web_bridge_launcher import (  # noqa: E402
    discover_saved_bridge_profiles,
    saved_web_bridge_session_id,
    web_bridge_mcp_endpoint,
)
from karox.web_bridge_profiles import WebBridgeProfileStore  # noqa: E402


DIAGNOSTICS_TOOL = wire_tool_name("karox.bridge.diagnostics")


@dataclass(frozen=True)
class CycleResult:
    client: int
    cycle: int
    endpoint_kind: str
    discovery: bool
    tool_count: int
    diagnostics_advertised: bool
    calls_passed: int
    controlled_error_isolated: bool
    initialize_ms: float
    list_ms: float
    first_call_ms: float
    remaining_calls_ms: float
    elapsed_ms: float
    error_class: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.discovery
            and self.tool_count > 0
            and self.diagnostics_advertised
            and self.calls_passed > 0
            and self.controlled_error_isolated
            and self.error_class is None
        )


def _diagnostics_payload(result: Any) -> dict[str, Any]:
    value = getattr(result, "structuredContent", None)
    if isinstance(value, dict):
        return value
    text = "".join(getattr(item, "text", "") for item in getattr(result, "content", ()))
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RuntimeError("diagnostics returned no structured object")
    return parsed


async def _cycle(
    *,
    endpoint: str,
    endpoint_kind: str,
    secret: str,
    client: int,
    cycle: int,
    calls: int,
    timeout_seconds: float,
) -> CycleResult:
    from mcp import ClientSession

    started = time.perf_counter()
    discovered = False
    tool_count = 0
    advertised = False
    calls_passed = 0
    controlled_error_isolated = False
    initialize_ms = 0.0
    list_ms = 0.0
    first_call_ms = 0.0
    remaining_calls_ms = 0.0
    try:
        headers = {"Authorization": f"Bearer {secret}"}
        async with streamable_http_transport(
            endpoint,
            headers=headers,
            timeout_seconds=timeout_seconds,
            terminate_on_close=False,
        ) as streams:
            async with ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            ) as session:
                phase_started = time.perf_counter()
                await session.initialize()
                initialize_ms = round((time.perf_counter() - phase_started) * 1000, 3)
                phase_started = time.perf_counter()
                listed = await session.list_tools()
                list_ms = round((time.perf_counter() - phase_started) * 1000, 3)
                names = [tool.name for tool in listed.tools]
                discovered = True
                tool_count = len(names)
                advertised = DIAGNOSTICS_TOOL in names
                if not advertised:
                    raise RuntimeError("diagnostics tool is not advertised")
                for call_index in range(calls):
                    phase_started = time.perf_counter()
                    result = await session.call_tool(DIAGNOSTICS_TOOL, {})
                    phase_ms = round((time.perf_counter() - phase_started) * 1000, 3)
                    if call_index == 0:
                        first_call_ms = phase_ms
                    else:
                        remaining_calls_ms += phase_ms
                    if bool(getattr(result, "isError", False)):
                        raise RuntimeError("diagnostics returned isError=true")
                    payload = _diagnostics_payload(result)
                    if not payload:
                        raise RuntimeError("diagnostics returned an empty payload")
                    calls_passed += 1
                # Exercise a controlled schema error on the same transport, then
                # prove the connector and shared runtime remain usable.
                rejected = await session.call_tool(
                    DIAGNOSTICS_TOOL,
                    {"unexpected_acceptance_field": True},
                )
                if not bool(getattr(rejected, "isError", False)):
                    raise RuntimeError("invalid arguments were not rejected")
                recovery = await session.call_tool(DIAGNOSTICS_TOOL, {})
                if bool(getattr(recovery, "isError", False)):
                    raise RuntimeError("controlled error poisoned the next invocation")
                if not _diagnostics_payload(recovery):
                    raise RuntimeError("recovery diagnostics returned an empty payload")
                controlled_error_isolated = True
        return CycleResult(
            client=client,
            cycle=cycle,
            endpoint_kind=endpoint_kind,
            discovery=discovered,
            tool_count=tool_count,
            diagnostics_advertised=advertised,
            calls_passed=calls_passed,
            controlled_error_isolated=controlled_error_isolated,
            initialize_ms=initialize_ms,
            list_ms=list_ms,
            first_call_ms=first_call_ms,
            remaining_calls_ms=round(remaining_calls_ms, 3),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    except Exception as exc:
        return CycleResult(
            client=client,
            cycle=cycle,
            endpoint_kind=endpoint_kind,
            discovery=discovered,
            tool_count=tool_count,
            diagnostics_advertised=advertised,
            calls_passed=calls_passed,
            controlled_error_isolated=controlled_error_isolated,
            initialize_ms=initialize_ms,
            list_ms=list_ms,
            first_call_ms=first_call_ms,
            remaining_calls_ms=round(remaining_calls_ms, 3),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            error_class=type(exc).__name__,
        )


async def _run(args: argparse.Namespace) -> list[CycleResult]:
    import anyio

    profile = WebBridgeProfileStore().get(args.saved)
    session_id = saved_web_bridge_session_id(profile.name)
    secret = BridgeCredentialStore().resolve(f"os-keyring:bridge/{session_id}")
    endpoints: list[tuple[str, str]] = []
    if args.endpoint in {"local", "both"}:
        endpoints.append(
            (
                "local",
                f"http://127.0.0.1:{profile.port}"
                f"{web_bridge_mcp_endpoint('', profile.target_profile)}",
            )
        )
    if args.endpoint in {"public", "both"}:
        public_url = profile.public_url
        if not public_url:
            public_url = next(
                (
                    str(item.get("public_url") or "")
                    for item in discover_saved_bridge_profiles()
                    if item.get("saved_profile") == profile.name and item.get("public_url")
                ),
                "",
            )
        if not public_url:
            raise RuntimeError("saved profile has no public URL")
        endpoints.append(
            (
                "public",
                web_bridge_mcp_endpoint(public_url, profile.target_profile),
            )
        )

    results: list[CycleResult] = []
    result_lock = anyio.Lock()

    async def run_client(client: int, endpoint_kind: str, endpoint: str) -> None:
        for cycle in range(1, args.cycles + 1):
            result = await _cycle(
                endpoint=endpoint,
                endpoint_kind=endpoint_kind,
                secret=secret,
                client=client,
                cycle=cycle,
                calls=args.calls,
                timeout_seconds=args.timeout_seconds,
            )
            async with result_lock:
                results.append(result)

    async with anyio.create_task_group() as group:
        for endpoint_kind, endpoint in endpoints:
            for client in range(1, args.clients + 1):
                group.start_soon(run_client, client, endpoint_kind, endpoint)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved", default="chatgpt-dev")
    parser.add_argument("--endpoint", choices=("local", "public", "both"), default="both")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--calls", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import anyio

    args = _parser().parse_args(argv)
    if args.cycles < 1 or args.clients < 1 or args.calls < 1:
        raise SystemExit("cycles, clients, and calls must be positive")
    results = anyio.run(_run, args)
    results.sort(key=lambda item: (item.endpoint_kind, item.client, item.cycle))
    passed = sum(item.passed for item in results)
    payload = {
        "saved_profile": args.saved,
        "credential_exposed": False,
        "passed": passed,
        "total": len(results),
        "all_passed": passed == len(results),
        "results": [asdict(item) | {"passed": item.passed} for item in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
