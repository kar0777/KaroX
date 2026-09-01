"""Detached worker serving one private mobile Mission Control dashboard."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .mission_control import MissionControlStore
from .mission_control_server import MissionControlServer

_SCHEMA_VERSION = 1
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("mobile Mission Control request is malformed")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = _request(args.request_file.expanduser().resolve(strict=True))
    run_id = str(payload.get("run_id") or "")
    host = str(payload.get("host") or "")
    ready = Path(str(payload.get("ready_file") or "")).expanduser().resolve()
    stop = Path(str(payload.get("stop_file") or "")).expanduser().resolve()
    address = ipaddress.ip_address(host)
    if address.version != 4 or address not in _TAILSCALE_V4:
        raise ValueError("mobile Mission Control worker refuses non-Tailscale host")
    if not run_id or not ready.name or not stop.name:
        raise ValueError("mobile Mission Control request is incomplete")

    store = MissionControlStore(run_id)
    if store.snapshot() is None:
        raise ValueError("Mission Control run does not exist")
    artifacts = ArtifactStore(run_id)
    server = MissionControlServer(
        store,
        host=host,
        port=0,
        image_reader=artifacts.read_image,
    )
    bound_host, bound_port = server.bind()
    if bound_host != host:
        server.shutdown()
        raise ValueError("mobile Mission Control bound an unexpected host")
    _atomic_json(
        ready,
        {
            "schema_version": _SCHEMA_VERSION,
            "host": bound_host,
            "port": bound_port,
            "pairing_code": server.pairing.code,
            "pairing_expires_at": server.pairing.expires_at,
        },
    )

    thread = threading.Thread(target=server.serve_forever, name=f"karox-mobile-{run_id}")
    thread.start()
    try:
        while thread.is_alive() and not stop.exists():
            time.sleep(0.2)
    finally:
        try:
            stop.unlink()
        except FileNotFoundError:
            pass
        server.shutdown()
        thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":  # pragma: no cover - detached process entry point
    raise SystemExit(main())
