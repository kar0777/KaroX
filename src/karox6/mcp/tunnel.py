"""An HTTPS address for the local MCP endpoint through Tailscale.

Funnel publishes it on the internet (ChatGPT, Claude and other hosted connectors reach
it); Serve keeps it inside your tailnet (your own devices only). Tailscale terminates TLS
with a real certificate for ``<machine>.<tailnet>.ts.net`` and proxies to 127.0.0.1, so
KaroX itself never listens on a public interface. The proxy is configured in the
background (``--bg``) and removed again when KaroX stops; only the HTTPS port KaroX took
is touched, routes other programs configured stay as they are.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

FUNNEL_PORTS = (443, 8443, 10000)  # the only HTTPS ports Funnel accepts
TIMED_OUT = -1
_ENABLE_URL = re.compile(r"https://login\.tailscale\.com/\S+")

Runner = Callable[[list[str], float], tuple[int, str]]


class TunnelError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Tunnel:
    url: str  # https://host[:port], no trailing slash
    public: bool
    stop: Callable[[], None]


def run_cli(argv: list[str], timeout_s: float) -> tuple[int, str]:
    """(exit code, stdout + stderr); TIMED_OUT when the command did not finish in time."""
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return TIMED_OUT, partial
    except OSError as exc:
        raise TunnelError(f"could not run {argv[0]}: {exc}") from exc
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def find_tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    candidates: list[str] = []
    if sys.platform == "win32":
        for var in ("PROGRAMFILES", "PROGRAMW6432"):
            if os.environ.get(var):
                candidates.append(os.path.join(os.environ[var], "Tailscale", "tailscale.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    return next((c for c in candidates if os.path.isfile(c)), None)


def open_tailscale(
    local_port: int, *, public: bool = True, exe: str | None = None, run: Runner = run_cli
) -> Tunnel:
    """Route ``https://<this machine>.ts.net[:port]/`` to ``http://127.0.0.1:<local_port>``."""
    exe = exe or find_tailscale()
    if not exe:
        raise TunnelError(
            "Tailscale is not installed (or not on PATH): get it from "
            "https://tailscale.com/download, sign in, then start KaroX again"
        )
    code, out = run([exe, "status", "--json"], 20)
    status = _json_object(out) if code == 0 else None
    if status is None:
        raise TunnelError(
            "Tailscale does not answer: open the Tailscale app (or run `tailscale up`). "
            + _short(out)
        )
    state = status.get("BackendState")
    if state != "Running":
        raise TunnelError(
            f"Tailscale is not connected (state: {state or 'unknown'}): open the Tailscale "
            "app and sign in, or run `tailscale up`"
        )
    me = status.get("Self")
    host = str(me.get("DNSName") or "").rstrip(".") if isinstance(me, dict) else ""
    if not host:
        raise TunnelError(
            "this machine has no tailnet DNS name: turn on MagicDNS and HTTPS certificates "
            "on the DNS page of the Tailscale admin console"
        )
    target = f"http://127.0.0.1:{local_port}"
    code, out = run([exe, "serve", "status", "--json"], 20)
    config = (_json_object(out) or {}) if code == 0 else {}
    port = free_port(config, host, target, public=public)
    if port is None:
        raise TunnelError(
            "the HTTPS ports Tailscale can publish (443, 8443, 10000) all serve something "
            "else already (see `tailscale serve status`); free one with "
            "`tailscale funnel --https=<port> off`"
        )
    kind = "funnel" if public else "serve"
    code, out = run([exe, kind, "--bg", "--yes", f"--https={port}", target], 60)
    if code != 0:
        raise TunnelError(explain_failure(kind, code, out))
    url = f"https://{host}" if port == 443 else f"https://{host}:{port}"

    def stop() -> None:
        with contextlib.suppress(TunnelError):
            run([exe, kind, f"--https={port}", target, "off"], 30)

    return Tunnel(url=url, public=public, stop=stop)


def free_port(config: dict[str, Any], host: str, target: str, *, public: bool) -> int | None:
    """The first Funnel-capable HTTPS port that is unused, or already routes only "/" to
    ``target`` with the same visibility (left over from a KaroX that was killed)."""
    tcp, web, funnel = (_section(config, name) for name in ("TCP", "Web", "AllowFunnel"))
    for port in FUNNEL_PORTS:
        key = f"{host}:{port}"
        if str(port) not in tcp and not any(k.endswith(f":{port}") for k in web):
            return port
        handlers = web.get(key, {}).get("Handlers") if isinstance(web.get(key), dict) else None
        ours = handlers == {"/": {"Proxy": target}}
        if ours and bool(funnel.get(key)) == public:
            return port
    return None


def explain_failure(kind: str, code: int, output: str) -> str:
    lowered = output.lower()
    enable = _ENABLE_URL.search(output)
    if code == TIMED_OUT:
        reason = f"`tailscale {kind}` did not finish in time"
    elif enable or "not enabled" in lowered or "enable funnel" in lowered:
        reason = "Funnel is not enabled for this tailnet or machine"
    elif "not allowed" in lowered or "access denied" in lowered or "permission" in lowered:
        reason = (
            "Tailscale refused the change (on Linux run `sudo tailscale set --operator=$USER` once)"
        )
    elif "certificate" in lowered or ("https" in lowered and "disabled" in lowered):
        reason = "HTTPS certificates are off: turn them on in the Tailscale admin console"
    elif "login" in lowered or "logged out" in lowered:
        reason = "Tailscale is signed out: sign in with the app or `tailscale up`"
    else:
        reason = f"`tailscale {kind}` failed"
    hint = f" Open {enable.group(0)} to enable it." if enable else ""
    return f"{reason}.{hint} {_short(output)}".strip()


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return f"({text[:limit]})" if text else ""
