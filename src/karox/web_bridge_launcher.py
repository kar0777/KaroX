"""One-command lifecycle manager for ChatGPT/Claude web MCP bridges."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from .bridge import BridgeCredentialStore
from .models import AccessProfile
from .paths import runtime_dir, session_dir
from .sessions import SessionStore


WEB_BRIDGE_PROFILES = ("chatgpt-web", "claude-web")
DEFAULT_WEB_TOOLS = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
)
WRITE_WEB_TOOLS = (
    "karox.repo.edit_file",
    "karox.repo.write_file",
)
_QUICK_TUNNEL_URL = re.compile(
    r"https://[A-Za-z0-9-]+\.trycloudflare\.com(?=$|[\s/])"
)


class WebBridgeLaunchError(RuntimeError):
    """A managed web bridge could not be prepared or kept alive."""


@dataclass(frozen=True)
class WebBridgeConnectConfig:
    profile: str
    repository: Path
    port: int = 8765
    tools: tuple[str, ...] = DEFAULT_WEB_TOOLS
    session_id: Optional[str] = None
    access_profile: AccessProfile = AccessProfile.WORKSPACE_WRITE
    tunnel: str = "cloudflare"
    public_url: Optional[str] = None
    cloudflared: Optional[str] = None
    tunnel_timeout_seconds: float = 30.0
    deadline_seconds: float = 30.0
    verification_commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.profile not in WEB_BRIDGE_PROFILES:
            raise ValueError("web bridge profile must be chatgpt-web or claude-web")
        if not 1 <= self.port <= 65_535:
            raise ValueError("web bridge port must be between 1 and 65535")
        if self.tunnel not in {"cloudflare", "custom"}:
            raise ValueError("web bridge tunnel must be cloudflare or custom")
        if self.tunnel == "custom" and not self.public_url:
            raise ValueError("custom web bridge tunnel requires --public-url")
        if self.tunnel == "cloudflare" and self.public_url:
            raise ValueError(
                "--public-url is determined automatically for a Cloudflare Quick Tunnel"
            )
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "--public-url must be an HTTPS origin without a path, query, "
                    "fragment, or user information"
                )
        if not self.tools or len(set(self.tools)) != len(self.tools):
            raise ValueError("web bridge tools must be non-empty and unique")
        if not 1.0 <= float(self.tunnel_timeout_seconds) <= 300.0:
            raise ValueError("tunnel timeout must be between 1 and 300 seconds")
        if not 0.1 <= float(self.deadline_seconds) <= 3600.0:
            raise ValueError("bridge deadline must be between 0.1 and 3600 seconds")


@dataclass
class CloudflareQuickTunnel:
    process: subprocess.Popen[str]
    public_url: str
    output_tail: deque[str]
    reader: threading.Thread

    def stop(self) -> None:
        _stop_process(self.process)
        self.reader.join(timeout=2)


def find_cloudflared(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve cloudflared from an explicit path, PATH, or KaroX runtime bin."""
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return str(candidate) if candidate.is_file() else None
    discovered = shutil.which("cloudflared")
    if discovered:
        return discovered
    executable = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    bundled = runtime_dir() / "bin" / executable
    return str(bundled) if bundled.is_file() else None


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _stop_process(process: Optional[subprocess.Popen[str]]) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def start_cloudflare_quick_tunnel(
    port: int,
    *,
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> CloudflareQuickTunnel:
    """Start cloudflared, parse its assigned HTTPS origin, and keep draining logs."""
    resolved = find_cloudflared(executable)
    if resolved is None:
        raise WebBridgeLaunchError(
            "cloudflared was not found; rerun the KaroX installer or pass "
            "--cloudflared PATH"
        )
    try:
        process = popen(
            [
                resolved,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise WebBridgeLaunchError(
            f"cannot start cloudflared: {type(exc).__name__}"
        ) from exc

    ready = threading.Event()
    state: dict[str, Optional[str]] = {"public_url": None}
    output_tail: deque[str] = deque(maxlen=30)

    def drain() -> None:
        stream = process.stdout
        if stream is None:
            ready.set()
            return
        for raw_line in stream:
            line = raw_line.rstrip()
            output_tail.append(line)
            match = _QUICK_TUNNEL_URL.search(line)
            if match and state["public_url"] is None:
                state["public_url"] = match.group(0)
                ready.set()
        ready.set()

    reader = threading.Thread(
        target=drain,
        name="karox-cloudflared-output",
        daemon=True,
    )
    reader.start()
    ready.wait(timeout_seconds)
    public_url = state["public_url"]
    if public_url is None:
        code = process.poll()
        _stop_process(process)
        reader.join(timeout=2)
        detail = next((line for line in reversed(output_tail) if line), "")
        suffix = f": {detail}" if detail else ""
        if code is None:
            raise WebBridgeLaunchError(
                f"cloudflared did not provide a public URL within {timeout_seconds:g}s"
                f"{suffix}"
            )
        raise WebBridgeLaunchError(f"cloudflared exited with code {code}{suffix}")
    return CloudflareQuickTunnel(process, public_url, output_tail, reader)


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _wait_for_bridge(
    process: subprocess.Popen[str],
    port: int,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise WebBridgeLaunchError(f"KaroX bridge exited with code {code}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise WebBridgeLaunchError("KaroX bridge did not open its local port")


def _session_id(config: WebBridgeConnectConfig) -> str:
    return config.session_id or (
        f"web-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    )


def _bridge_argv(
    config: WebBridgeConnectConfig,
    *,
    session_id: str,
    public_url: str,
) -> tuple[str, ...]:
    values = [
        sys.executable,
        "-m",
        "karox.cli",
        "bridge",
        "serve",
        "--profile",
        config.profile,
        "--protocol",
        "mcp",
        "--public-url",
        public_url,
        "--repository",
        str(config.repository),
        "--session-id",
        session_id,
        "--credential",
        session_id,
        "--port",
        str(config.port),
        "--deadline-seconds",
        str(config.deadline_seconds),
    ]
    for tool in config.tools:
        values.extend(("--tool", tool))
    for command in config.verification_commands:
        values.extend(("--verification-command", command))
    return tuple(values)


def run_web_bridge(config: WebBridgeConnectConfig) -> int:
    """Own the tunnel and bridge processes until Ctrl+C or either child exits."""
    repository = config.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise WebBridgeLaunchError("web bridge repository must be a directory")
    config = replace(config, repository=repository)
    if not _port_is_available(config.port):
        raise WebBridgeLaunchError(f"local bridge port is already in use: {config.port}")

    tunnel: Optional[CloudflareQuickTunnel] = None
    bridge: Optional[subprocess.Popen[str]] = None
    credential_created = False
    session_created = False
    sessions: Optional[SessionStore] = None
    try:
        if config.tunnel == "cloudflare":
            tunnel = start_cloudflare_quick_tunnel(
                config.port,
                executable=config.cloudflared,
                timeout_seconds=config.tunnel_timeout_seconds,
            )
            public_url = tunnel.public_url
        else:
            assert config.public_url is not None
            public_url = config.public_url

        session_id = _session_id(config)
        sessions = SessionStore(session_dir())
        sessions.create(
            repository,
            f"{config.profile} managed web bridge",
            config.access_profile,
            session_id=session_id,
        )
        session_created = True
        credential = BridgeCredentialStore().set(session_id)
        credential_created = True
        secret = credential.get("secret")
        if not isinstance(secret, str) or not secret:
            raise WebBridgeLaunchError("bridge credential generator returned no secret")

        try:
            bridge = subprocess.Popen(
                _bridge_argv(
                    config,
                    session_id=session_id,
                    public_url=public_url,
                ),
                cwd=repository,
                creationflags=_creation_flags(),
            )
        except OSError as exc:
            raise WebBridgeLaunchError(
                f"cannot start KaroX bridge: {type(exc).__name__}"
            ) from exc
        _wait_for_bridge(bridge, config.port)

        endpoint = f"{public_url.rstrip('/')}/mcp"
        print(f"KaroX {config.profile} bridge is ready")
        print(f"MCP URL: {endpoint}")
        print(f"OAuth approval password: {secret}")
        print(f"Session: {session_id}")
        if config.profile == "chatgpt-web":
            print("Add the MCP URL as a custom app in ChatGPT developer mode.")
        else:
            print("Add the MCP URL in Claude Settings > Connectors.")
        print("Press Ctrl+C to stop the bridge and tunnel.", flush=True)

        while True:
            bridge_code = bridge.poll()
            if bridge_code is not None:
                raise WebBridgeLaunchError(
                    f"KaroX bridge stopped unexpectedly with code {bridge_code}"
                )
            if tunnel is not None:
                tunnel_code = tunnel.process.poll()
                if tunnel_code is not None:
                    raise WebBridgeLaunchError(
                        f"Cloudflare tunnel stopped unexpectedly with code {tunnel_code}"
                    )
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping KaroX web bridge…", flush=True)
        return 0
    finally:
        _stop_process(bridge)
        if tunnel is not None:
            tunnel.stop()
        if credential_created:
            try:
                BridgeCredentialStore().delete(session_id)
            except Exception:
                pass
        if session_created and sessions is not None:
            try:
                sessions.revoke(session_id)
            except Exception:
                pass
