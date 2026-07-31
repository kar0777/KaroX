"""Thin standard-library client for a local KaroX remote workspace."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


MAX_RESPONSE_BYTES = 5_000_000


class RemoteClientError(RuntimeError):
    """A sanitized error that never contains connection values."""


@dataclass(frozen=True)
class RemoteEnvironment:
    url: str = field(repr=False)
    credential: str = field(repr=False)
    session_id: str

    @classmethod
    def load(cls) -> "RemoteEnvironment":
        url = os.environ.get("KAROX_REMOTE_URL", "").strip()
        credential = os.environ.get("KAROX_REMOTE_CREDENTIAL", "").strip()
        session_id = os.environ.get("KAROX_SESSION_ID", "").strip()
        if not url or not credential or not session_id:
            raise RemoteClientError(
                "KaroX remote connection variables are unavailable"
            )
        if (
            not url.startswith("https://")
            or any(char in url for char in "\r\n\0")
            or any(char in credential for char in "\r\n\0")
            or any(char in session_id for char in "\r\n\0")
        ):
            raise RemoteClientError("KaroX remote connection variables are malformed")
        return cls(url.rstrip("/"), credential, session_id)


class KaroXRemoteClient:
    def __init__(
        self,
        environment: Optional[RemoteEnvironment] = None,
        *,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.environment = environment or RemoteEnvironment.load()
        self.timeout_seconds = float(timeout_seconds)

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        if not path.startswith("/") or "://" in path:
            raise RemoteClientError("remote API path is malformed")
        payload = (
            json.dumps(
                dict(body), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            if body is not None
            else None
        )
        request_headers = {
            "Authorization": f"Bearer {self.environment.credential}",
            "X-KaroX-Remote-Protocol": "1",
            "X-KaroX-Session-ID": self.environment.session_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "karox-remote/5",
        }
        request_headers.update(dict(headers or {}))
        request = urllib.request.Request(
            self.environment.url + path,
            data=payload,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read(100_000)
                decoded = json.loads(raw.decode("utf-8", errors="replace"))
                code = decoded.get("error") if isinstance(decoded, dict) else None
            except Exception:
                code = None
            suffix = (
                f" ({code})"
                if isinstance(code, str) and 0 < len(code) <= 120
                else ""
            )
            raise RemoteClientError(
                f"local KaroX bridge rejected the request: HTTP {status}{suffix}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RemoteClientError("local KaroX bridge is unreachable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RemoteClientError("local KaroX bridge response is too large")
        if status != 200:
            raise RemoteClientError(
                f"local KaroX bridge rejected the request: HTTP {status}"
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RemoteClientError(
                "local KaroX bridge returned malformed JSON"
            ) from None
        if not isinstance(decoded, dict):
            raise RemoteClientError(
                "local KaroX bridge returned a non-object response"
            )
        return decoded

    def get(self, path: str) -> Mapping[str, Any]:
        return self._request("GET", path)

    def call(
        self,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 600.0,
    ) -> Mapping[str, Any]:
        if not tool_name.startswith("karox."):
            raise RemoteClientError("tool name must be in the KaroX namespace")
        if not 0.1 <= float(deadline_seconds) <= 3600.0:
            raise RemoteClientError("deadline_seconds must be between 0.1 and 3600")
        headers = (
            {"X-KaroX-Idempotency-Key": idempotency_key}
            if idempotency_key
            else None
        )
        return self._request(
            "POST",
            "/tools/" + urllib.parse.quote(tool_name, safe=".-_"),
            dict(arguments or {}),
            headers=headers,
        )

    def preflight(self) -> Mapping[str, Any]:
        diagnostics = self.get("/diagnostics")
        session = self.get("/session")
        observed = session.get("session_id")
        if observed != self.environment.session_id:
            raise RemoteClientError("local KaroX session binding does not match")
        return {
            "ok": True,
            "session": session,
            "diagnostics": diagnostics,
            "tools": self.get("/tools").get("tools", []),
        }


def _json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("value must be valid JSON") from exc


def _argv_json(value: str) -> list[str]:
    payload = _json(value)
    if (
        not isinstance(payload, list)
        or not payload
        or not all(isinstance(item, str) and item for item in payload)
    ):
        raise argparse.ArgumentTypeError("argv must be a JSON array of strings")
    return payload


def _common_call_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idempotency-key")
    parser.add_argument("--deadline-seconds", type=float, default=600.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karox-remote")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("context")
    sub.add_parser("list")

    read = sub.add_parser("read")
    read.add_argument("path")

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--pattern", default="**/*")
    search.add_argument("--regex", action="store_true")
    search.add_argument("--case-sensitive", action="store_true")
    search.add_argument("--max-results", type=int, default=200)

    patch = sub.add_parser("patch")
    patch.add_argument("path")
    patch.add_argument("--old", required=True)
    patch.add_argument("--new", required=True)
    patch.add_argument("--expected-occurrences", type=int, default=1)
    patch.add_argument("--expected-sha256")
    _common_call_options(patch)

    write = sub.add_parser("write")
    write.add_argument("path")
    write.add_argument("--content", required=True)
    _common_call_options(write)

    for name in ("command", "check", "process-start"):
        item = sub.add_parser(name)
        item.add_argument("argv", type=_argv_json)
        if name == "process-start":
            item.add_argument("--process-id")
        _common_call_options(item)

    process_status = sub.add_parser("process-status")
    process_status.add_argument("process_id")
    process_stop = sub.add_parser("process-stop")
    process_stop.add_argument("process_id")
    _common_call_options(process_stop)

    browser = sub.add_parser("browser")
    browser.add_argument("url")
    browser.add_argument("--actions", type=_json, default=[])
    browser.add_argument("--screenshot-path")
    browser.add_argument("--width", type=int, default=1440)
    browser.add_argument("--height", type=int, default=900)
    _common_call_options(browser)

    screenshot = sub.add_parser("screenshot")
    screenshot.add_argument("url")
    screenshot.add_argument("path")
    screenshot.add_argument("--width", type=int, default=1440)
    screenshot.add_argument("--height", type=int, default=900)
    _common_call_options(screenshot)

    sub.add_parser("git-status")
    diff = sub.add_parser("diff")
    diff.add_argument("--staged", action="store_true")
    diff.add_argument("--paths", nargs="*", default=[])
    log = sub.add_parser("git-log")
    log.add_argument("--limit", type=int, default=20)

    commit = sub.add_parser("commit")
    commit.add_argument("message")
    commit.add_argument("paths", nargs="+")
    _common_call_options(commit)

    sub.add_parser("checkpoint")
    sub.add_parser("report")

    generic = sub.add_parser("call")
    generic.add_argument("tool")
    generic.add_argument("--arguments", type=_json, default={})
    _common_call_options(generic)
    return parser


_MUTATING = {
    "karox.repo.write_file",
    "karox.repo.patch",
    "karox.command.run",
    "karox.checks.run",
    "karox.process.start",
    "karox.process.stop",
    "karox.browser.actions",
    "karox.git.commit",
}


def _key(args: argparse.Namespace, tool: str) -> Optional[str]:
    supplied = getattr(args, "idempotency_key", None)
    if supplied:
        return supplied
    return f"remote-{uuid.uuid4().hex}" if tool in _MUTATING else None


def dispatch(
    client: KaroXRemoteClient, args: argparse.Namespace
) -> Mapping[str, Any]:
    command = args.command
    if command == "preflight":
        return client.preflight()
    if command == "context":
        return client.get("/context/brief")
    if command == "list":
        return client.get("/tools")
    if command == "read":
        return client.call("karox.repo.read_file", {"path": args.path})
    if command == "search":
        return client.call(
            "karox.repo.search",
            {
                "query": args.query,
                "pattern": args.pattern,
                "regex": args.regex,
                "case_sensitive": args.case_sensitive,
                "max_results": args.max_results,
            },
        )
    if command == "patch":
        arguments: dict[str, Any] = {
            "path": args.path,
            "old_string": args.old,
            "new_string": args.new,
            "expected_occurrences": args.expected_occurrences,
        }
        if args.expected_sha256:
            arguments["expected_sha256"] = args.expected_sha256
        tool = "karox.repo.patch"
        return client.call(
            tool,
            arguments,
            idempotency_key=_key(args, tool),
            deadline_seconds=args.deadline_seconds,
        )
    if command == "write":
        tool = "karox.repo.write_file"
        return client.call(
            tool,
            {"path": args.path, "content": args.content},
            idempotency_key=_key(args, tool),
            deadline_seconds=args.deadline_seconds,
        )
    if command in {"command", "check", "process-start"}:
        tool = {
            "command": "karox.command.run",
            "check": "karox.checks.run",
            "process-start": "karox.process.start",
        }[command]
        arguments = {"argv": args.argv}
        if command == "process-start" and args.process_id:
            arguments["process_id"] = args.process_id
        return client.call(
            tool,
            arguments,
            idempotency_key=_key(args, tool),
            deadline_seconds=args.deadline_seconds,
        )
    if command == "process-status":
        return client.call(
            "karox.process.status", {"process_id": args.process_id}
        )
    if command == "process-stop":
        tool = "karox.process.stop"
        return client.call(
            tool,
            {"process_id": args.process_id},
            idempotency_key=_key(args, tool),
            deadline_seconds=args.deadline_seconds,
        )
    if command in {"browser", "screenshot"}:
        tool = "karox.browser.actions"
        arguments = {
            "url": args.url,
            "actions": args.actions if command == "browser" else [],
            "screenshot_path": (
                args.screenshot_path if command == "browser" else args.path
            ),
            "width": args.width,
            "height": args.height,
        }
        if arguments["screenshot_path"] is None:
            arguments.pop("screenshot_path")
        return client.call(
            tool,
            arguments,
            idempotency_key=_key(args, tool),
            deadline_seconds=args.deadline_seconds,
        )
    if command == "git-status":
        return client.call("karox.git.status")
    if command == "diff":
        return client.call(
            "karox.git.diff",
            {"staged": args.staged, "paths": args.paths},
        )
    if command == "git-log":
        return client.call("karox.git.log", {"limit": args.limit})
    if command == "commit":
        tool = "karox.git.commit"
        return client.call(
            tool,
            {"message": args.message, "paths": args.paths},
            idempotency_key=_key(args, tool),
            deadline_seconds=args.deadline_seconds,
        )
    if command == "checkpoint":
        report = client.call("karox.report.get")
        return {
            "ok": True,
            "detail": (
                "A local checkpoint was created before the Ellipsis session. "
                "Rollback is user-only through Karo CLI."
            ),
            "report": report,
        }
    if command == "report":
        return client.call("karox.report.get")
    if command == "call":
        if not isinstance(args.arguments, dict):
            raise RemoteClientError("--arguments must be a JSON object")
        return client.call(
            args.tool,
            args.arguments,
            idempotency_key=_key(args, args.tool),
            deadline_seconds=args.deadline_seconds,
        )
    raise RemoteClientError("unknown command")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = dispatch(KaroXRemoteClient(), args)
    except (RemoteClientError, ValueError) as exc:
        print(f"karox-remote error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
