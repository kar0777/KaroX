"""Local process, tunnel and bridge helpers for Ellipsis-backed KaroX agents."""

from __future__ import annotations

import os
import queue
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import httpx

from . import __version__
from .core import _new_process_group_kwargs
from .remote_tools import _kill_pid_tree
from .tailscale import TailscaleError, prepare_tailscale_funnel
from .web_bridge_launcher import (
    WebBridgeLaunchError,
    cloudflared_not_found_message,
    find_cloudflared,
)


class EllipsisHelperError(ValueError):
    """A helper failure that is safe to display without connection values."""


_QUICK_TUNNEL_URL = re.compile(
    r"https://[A-Za-z0-9-]+\.trycloudflare\.com(?=$|[\s/])"
)


@dataclass
class QuietTunnel:
    process: Optional[subprocess.Popen[str]]
    public_url: str = field(repr=False)
    reader: Optional[threading.Thread] = None

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process is not None else None

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            _kill_pid_tree(self.process.pid)
        if self.reader is not None:
            self.reader.join(timeout=2.0)


def detachable_child_options() -> dict[str, Any]:
    return _new_process_group_kwargs()


def port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _quiet_process(
    argv: Sequence[str],
) -> tuple[subprocess.Popen[str], queue.Queue[Optional[str]], threading.Thread]:
    try:
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **detachable_child_options(),
        )
    except OSError as exc:
        raise EllipsisHelperError(
            f"cannot start local helper process: {type(exc).__name__}"
        ) from None
    lines: queue.Queue[Optional[str]] = queue.Queue()

    def drain() -> None:
        stream = process.stdout
        if stream is None:
            lines.put(None)
            return
        for raw in stream:
            lines.put(raw.rstrip())
        lines.put(None)

    reader = threading.Thread(
        target=drain,
        name=f"karox-helper-{process.pid}",
        daemon=True,
    )
    reader.start()
    return process, lines, reader


def _await_line(
    process: subprocess.Popen[str],
    lines: queue.Queue[Optional[str]],
    *,
    timeout_seconds: float,
    predicate: Any,
) -> Optional[str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None and lines.empty():
            return None
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            return None
        if predicate(line):
            return line
    return None


def start_cloudflare_tunnel(port: int) -> QuietTunnel:
    executable = find_cloudflared()
    if executable is None:
        raise WebBridgeLaunchError(cloudflared_not_found_message())
    process, lines, reader = _quiet_process(
        [
            executable,
            "tunnel",
            "--url",
            f"http://127.0.0.1:{port}",
            "--no-autoupdate",
        ]
    )
    line = _await_line(
        process,
        lines,
        timeout_seconds=30.0,
        predicate=lambda value: _QUICK_TUNNEL_URL.search(value) is not None,
    )
    match = _QUICK_TUNNEL_URL.search(line or "")
    if match is None:
        _kill_pid_tree(process.pid)
        reader.join(timeout=2.0)
        raise EllipsisHelperError("Cloudflare tunnel did not become ready")
    return QuietTunnel(process, match.group(0), reader)


def start_tailscale_tunnel(port: int) -> QuietTunnel:
    try:
        plan = prepare_tailscale_funnel(port)
    except TailscaleError as exc:
        raise EllipsisHelperError(str(exc)) from None
    process, lines, reader = _quiet_process(plan.argv)
    line = _await_line(
        process,
        lines,
        timeout_seconds=30.0,
        predicate=lambda value: plan.public_url in value or ".ts.net" in value,
    )
    if line is None:
        _kill_pid_tree(process.pid)
        reader.join(timeout=2.0)
        raise EllipsisHelperError("Tailscale Funnel did not confirm its endpoint")
    return QuietTunnel(process, plan.public_url, reader)


def start_tunnel(
    kind: str,
    *,
    port: int,
    public_url: Optional[str] = None,
) -> QuietTunnel:
    if kind == "custom":
        if not public_url or not public_url.startswith("https://"):
            raise EllipsisHelperError("custom tunnel requires an HTTPS public URL")
        return QuietTunnel(None, public_url)
    if kind == "cloudflare":
        return start_cloudflare_tunnel(port)
    if kind == "tailscale":
        return start_tailscale_tunnel(port)
    raise EllipsisHelperError("unsupported tunnel type")


def git_text(repository: Path, argv: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *argv],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
            **detachable_child_options(),
        )
    except (OSError, subprocess.SubprocessError):
        raise EllipsisHelperError("local Git preflight failed") from None
    if completed.returncode != 0:
        raise EllipsisHelperError(
            f"local Git preflight failed with exit code {completed.returncode}"
        )
    return completed.stdout.strip()


def infer_command_allowlists(repository: Path) -> tuple[tuple[str, ...], ...]:
    """Return conservative test/build/dev prefixes inferred from project files."""
    result: list[tuple[str, ...]] = []
    if any(
        (repository / name).exists()
        for name in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini")
    ):
        result.extend(
            [
                ("python", "-m", "pytest", "*"),
                ("python", "-m", "unittest", "*"),
                ("python", "-m", "compileall", "*"),
                ("python", "-m", "ruff", "*"),
                ("python", "-m", "mypy", "*"),
            ]
        )
    if (repository / "package.json").is_file():
        manager = (
            "pnpm"
            if (repository / "pnpm-lock.yaml").exists()
            else "yarn"
            if (repository / "yarn.lock").exists()
            else "npm"
        )
        if manager == "npm":
            result.extend(
                [
                    ("npm", "test", "*"),
                    ("npm", "run", "test", "*"),
                    ("npm", "run", "lint", "*"),
                    ("npm", "run", "typecheck", "*"),
                    ("npm", "run", "build", "*"),
                    ("npm", "run", "dev", "*"),
                ]
            )
        else:
            result.extend(
                [
                    (manager, "test", "*"),
                    (manager, "lint", "*"),
                    (manager, "typecheck", "*"),
                    (manager, "build", "*"),
                    (manager, "dev", "*"),
                ]
            )
    if (repository / "Cargo.toml").is_file():
        result.extend(
            [
                ("cargo", "test", "*"),
                ("cargo", "check", "*"),
                ("cargo", "clippy", "*"),
                ("cargo", "build", "*"),
                ("cargo", "run", "*"),
            ]
        )
    if (repository / "go.mod").is_file():
        result.extend(
            [
                ("go", "test", "*"),
                ("go", "build", "*"),
                ("go", "run", "*"),
            ]
        )
    if any(repository.glob("*.sln")) or any(repository.rglob("*.csproj")):
        result.extend(
            [
                ("dotnet", "test", "*"),
                ("dotnet", "build", "*"),
                ("dotnet", "run", "*"),
            ]
        )
    return tuple(dict.fromkeys(result))


def remote_install_manifest() -> dict[str, Any]:
    url = os.environ.get("KAROX_REMOTE_INSTALL_URL", "").strip()
    digest = os.environ.get("KAROX_REMOTE_INSTALL_SHA256", "").strip().lower()
    version = os.environ.get("KAROX_REMOTE_PACKAGE_VERSION", "").strip()
    if url or digest:
        if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise EllipsisHelperError(
                "standalone karox-remote requires a pinned HTTPS URL and SHA-256"
            )
        return {
            "strategy": "standalone",
            "url": url,
            "sha256": digest,
            "command": "karox-remote",
        }
    if version:
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[A-Za-z0-9._-]+)?", version):
            raise EllipsisHelperError(
                "KAROX_REMOTE_PACKAGE_VERSION must be an exact version"
            )
        return {
            "strategy": "python-package",
            "package": f"karox-remote=={version}",
            "command": "karox-remote",
        }
    return {
        "strategy": "preinstalled",
        "command": "karox-remote",
        "required_version": __version__,
    }


class LocalBridgeClient:
    """Call the local bridge without displaying its URL or credential."""

    def __init__(
        self,
        *,
        remote_url: str,
        credential: str,
        local_session_id: str,
        timeout_seconds: float = 600.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=remote_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {credential}",
                "X-KaroX-Remote-Protocol": "1",
                "X-KaroX-Session-ID": local_session_id,
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path: str) -> Mapping[str, Any]:
        try:
            response = self._client.get(path)
        except httpx.HTTPError:
            raise EllipsisHelperError("local KaroX bridge request failed") from None
        if response.status_code != 200:
            raise EllipsisHelperError(
                f"local KaroX bridge rejected the request ({response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError:
            raise EllipsisHelperError("local KaroX bridge response is malformed") from None
        if not isinstance(payload, dict):
            raise EllipsisHelperError("local KaroX bridge response is malformed")
        return payload

    def call(
        self,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 600.0,
    ) -> Mapping[str, Any]:
        if not 0.1 <= float(deadline_seconds) <= 3600.0:
            raise EllipsisHelperError("local bridge deadline is outside safe bounds")
        headers = (
            {"X-KaroX-Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        try:
            response = self._client.post(
                f"/tools/{tool_name}",
                json=dict(arguments or {}),
                headers=headers,
            )
        except httpx.HTTPError:
            raise EllipsisHelperError("local KaroX bridge request failed") from None
        try:
            payload = response.json()
        except ValueError:
            raise EllipsisHelperError("local KaroX bridge response is malformed") from None
        if response.status_code != 200:
            error = payload.get("error") if isinstance(payload, dict) else None
            suffix = f": {error}" if isinstance(error, str) and len(error) < 200 else ""
            raise EllipsisHelperError(
                f"local KaroX tool call failed ({response.status_code}){suffix}"
            )
        if not isinstance(payload, dict):
            raise EllipsisHelperError("local KaroX bridge response is malformed")
        return payload


def validate_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise EllipsisHelperError("remote transport URL must use HTTPS")
    return value
