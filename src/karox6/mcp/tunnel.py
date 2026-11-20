"""An HTTPS address for the local MCP endpoint through Tailscale.

Funnel publishes it on the internet (ChatGPT, Claude and other hosted connectors reach
it); Serve keeps it inside your tailnet (your own devices only). Tailscale terminates TLS
with a real certificate for ``<machine>.<tailnet>.ts.net`` and proxies to 127.0.0.1, so
KaroX itself never listens on a public interface. The proxy is configured in the
background (``--bg``) and removed again when KaroX stops; only the HTTPS port KaroX took
is touched, routes other programs configured stay as they are. While KaroX runs,
``Tunnel.keep`` puts the route back if it vanished (Tailscale restarted, ``tailscale
serve reset``), again only onto a port that is free or still ours.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
    # Re-apply the route if it vanished; returns a line worth logging, or None when the
    # route is in place. Call it from a thread, like stop.
    keep: Callable[[], str | None]


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
    local_port: int,
    *,
    public: bool = True,
    local_host: str = "127.0.0.1",
    exe: str | None = None,
    run: Runner = run_cli,
    mount: str = "/karox6",
) -> Tunnel:
    """Route ``https://<this machine>.ts.net[:port][/mount]/`` to
    ``http://<local_host>:<local_port>``; ``Tunnel.url`` ends with the mount when one was
    needed (the local server then accepts paths with and without it)."""
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
    if local_host in ("", "0.0.0.0", "::", "localhost"):
        local_host = "127.0.0.1"
    shown = f"[{local_host}]" if ":" in local_host else local_host
    target = f"http://{shown}:{local_port}"
    # Never guess: a config we could not read may hold another program's routes.
    config = _serve_config(exe, run)
    # Hosted connectors (ChatGPT) only reach the standard port 443. When another program
    # serves the root of 443, KaroX adds its own path there (`mount`) and leaves that
    # program's routes as they are; 8443 and 10000 are the last resort.
    found: int | None = 443
    path = "/"
    if not usable(config, host, 443, target, public=public):
        if mount and mount_free(config, host, mount, target, public=public):
            path = mount
        else:
            found = free_port(config, host, target, public=public)
    if found is None:
        raise TunnelError(
            "the HTTPS ports Tailscale can publish (443, 8443, 10000) all serve something "
            "else already (see `tailscale serve status`); free one with "
            "`tailscale funnel --https=<port> off`"
        )
    port = found
    kind = "funnel" if public else "serve"
    url = f"https://{host}" if port == 443 else f"https://{host}:{port}"
    url += "" if path == "/" else path
    publish = [exe, kind, "--bg", "--yes", f"--https={port}", target]
    if path != "/":
        publish[-1:-1] = [f"--set-path={path}"]

    def present(now: dict[str, Any]) -> bool:
        routes = _routes(now, host, port)
        return isinstance(routes, dict) and routes.get(path) == {"Proxy": target}

    def still_usable(now: dict[str, Any]) -> bool:
        if path == "/":
            return usable(now, host, port, target, public=public)
        return mount_free(now, host, path, target, public=public)

    # stop and keep run in threads: once stop has run, a keep still in flight or a later
    # one must not put the route back.
    lock = threading.Lock()
    stopped = False

    def stop() -> None:
        """Remove the route only while it is still exactly ours: another program may have
        replaced it meanwhile, and `off` would remove whatever serves the port."""
        nonlocal stopped
        with lock:
            stopped = True
            try:
                now = _serve_config(exe, run)
            except TunnelError:
                return
            mine = present(now) and (path != "/" or len(_routes(now, host, port)) == 1)
            if mine:
                with contextlib.suppress(TunnelError):
                    run([exe, kind, f"--https={port}", f"--set-path={path}", target, "off"], 30)

    def keep() -> str | None:
        with lock:
            if stopped:
                return None
            try:
                now = _serve_config(exe, run)
                if not still_usable(now):
                    return (
                        f"the Tailscale route on port {port} now serves something else; "
                        "KaroX leaves it alone, so the connector URL does not reach KaroX"
                    )
                if present(now):
                    return None  # still ours
                code, out = run(publish, 60)
            except TunnelError as exc:
                return str(exc)
            if code != 0:
                return "could not put the Tailscale route back: " + explain_failure(kind, code, out)
            return f"the Tailscale route had vanished; put it back ({url} -> {target})"

    code, out = run(publish, 60)
    if code != 0:
        if code == TIMED_OUT:  # it may have been applied after all
            stop()
        raise TunnelError(explain_failure(kind, code, out))
    return Tunnel(url=url, public=public, stop=stop, keep=keep)


def _serve_config(exe: str, run: Runner) -> dict[str, Any]:
    code, out = run([exe, "serve", "status", "--json"], 20)
    text = out.strip()
    config = {} if code == 0 and text in ("", "null", "{}") else _json_object(text)
    if code != 0 or config is None:
        raise TunnelError(
            "could not read the current Tailscale routes (`tailscale serve status --json`), "
            "so KaroX does not touch them. " + _short(out)
        )
    return config


def _routes(config: dict[str, Any], host: str, port: int) -> Any:
    entry = _section(config, "Web").get(f"{host}:{port}")
    return entry.get("Handlers") if isinstance(entry, dict) else None


def free_port(config: dict[str, Any], host: str, target: str, *, public: bool) -> int | None:
    """The first Funnel-capable HTTPS port KaroX may use (see `usable`)."""
    ports = (p for p in FUNNEL_PORTS if usable(config, host, p, target, public=public))
    return next(ports, None)


def usable(config: dict[str, Any], host: str, port: int, target: str, *, public: bool) -> bool:
    """``port`` is unused, or already routes only "/" to ``target`` with the same
    visibility (ours, or left over from a KaroX that was killed)."""
    # Routes of a foreground `tailscale funnel` (no --bg) live per session under
    # "Foreground"; their ports are never free and never ours to reuse.
    for session in _section(config, "Foreground").values():
        if isinstance(session, dict) and port in _ports(session):
            return False
    if port not in _ports(config):
        return True
    ours = _routes(config, host, port) == {"/": {"Proxy": target}}
    funnel = _section(config, "AllowFunnel")
    return ours and bool(funnel.get(f"{host}:{port}")) == public


def mount_free(config: dict[str, Any], host: str, mount: str, target: str, *, public: bool) -> bool:
    """Port 443 already serves web routes with the same visibility and ``mount`` on it is
    unused or already routes to ``target``: KaroX may add (or reuse) that one path."""
    for session in _section(config, "Foreground").values():
        if isinstance(session, dict) and 443 in _ports(session):
            return False
    tcp = _section(config, "TCP").get("443")
    routes = _routes(config, host, 443)
    if not isinstance(tcp, dict) or not tcp.get("HTTPS") or not isinstance(routes, dict):
        return False  # not a web listener (raw TCP forward) or not in a shape we know
    if bool(_section(config, "AllowFunnel").get(f"{host}:443")) != public:
        return False
    return routes.get(mount) in (None, {"Proxy": target})


def _ports(config: dict[str, Any]) -> set[int]:
    """HTTPS/TCP ports a serve config (or one foreground session of it) uses."""
    used = {int(p) for p in _section(config, "TCP") if str(p).isdigit()}
    for key in _section(config, "Web"):
        tail = str(key).rsplit(":", 1)[-1]
        if tail.isdigit():
            used.add(int(tail))
    return used


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
