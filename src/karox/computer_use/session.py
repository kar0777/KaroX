"""Durable per-session desktop state: bindings, artifacts, capture history.

The engine path is one CLI process per tool call, so attachments and capture
freshness must survive restarts. State lives under the runtime dir, one
directory per session id; a session never sees another session's bindings or
artifacts. Screenshots are stored as opaque artifact ids with sha256 digests —
evidence a model can cite without a path escape into arbitrary files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import DesktopSecurityError, InvalidActionError, TargetLostError
from .identity import WindowIdentity, capture_identity, refresh_identity

_APP_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_WINDOWS = 120
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class StoredBinding:
    app_id: str
    identity: WindowIdentity
    access: str
    attached_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "identity": self.identity.to_dict(),
            "access": self.access,
            "attached_at": self.attached_at,
        }


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


class DesktopSession:
    """One session's attachments and artifacts, pinned and re-verified."""

    def __init__(self, root: Path, session_id: str, probe: Any) -> None:
        if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
            raise InvalidActionError("session id must be a safe identifier")
        self.session_id = session_id
        self.probe = probe
        self.root = Path(root) / session_id
        self._bindings_path = self.root / "bindings.json"
        self._history_path = self.root / "capture_history.json"
        self._artifacts_dir = self.root / "artifacts"

    # -- bindings ------------------------------------------------------------
    def _load_bindings(self) -> dict[str, Any]:
        raw = _read_json(self._bindings_path, {})
        return raw if isinstance(raw, dict) else {}

    def _save_bindings(self, bindings: dict[str, Any]) -> None:
        _atomic_write_json(self._bindings_path, bindings)

    def list_windows(
        self, title_contains: Optional[str] = None, limit: int = _MAX_WINDOWS
    ) -> list[WindowIdentity]:
        needle = (title_contains or "").strip().casefold()
        rows: list[WindowIdentity] = []
        for raw in self.probe.enum_windows():
            if not raw.visible or not raw.title:
                continue
            if raw.rect[2] <= raw.rect[0] or raw.rect[3] <= raw.rect[1]:
                continue
            if needle and needle not in raw.title.casefold():
                continue
            rows.append(capture_identity(self.probe, raw.hwnd))
            if len(rows) >= max(1, min(int(limit), _MAX_WINDOWS)):
                break
        rows.sort(key=lambda item: item.width * item.height, reverse=True)
        return rows

    def attach(
        self,
        app_id: str,
        *,
        title_contains: Optional[str] = None,
        window_id: Optional[int] = None,
        access: str = "observe",
    ) -> StoredBinding:
        if not isinstance(app_id, str) or _APP_ID.fullmatch(app_id) is None:
            raise InvalidActionError("app_id must be a safe lowercase identifier")
        if access not in {"observe", "control"}:
            raise InvalidActionError("access must be observe or control")
        if window_id is not None:
            if isinstance(window_id, bool) or not isinstance(window_id, int) or window_id <= 0:
                raise InvalidActionError("window_id must be a positive integer")
            candidates = [
                capture_identity(self.probe, raw.hwnd)
                for raw in self.probe.enum_windows()
                if raw.hwnd == window_id
            ]
            if not candidates:
                raise TargetLostError(f"no open window has id {window_id}")
        else:
            needle = (title_contains or "").strip()
            if not needle:
                raise InvalidActionError("attach requires window_id or title_contains")
            candidates = self.list_windows(title_contains=needle, limit=8)
            if not candidates:
                raise TargetLostError(f"no visible window matches {needle!r}")
        identity = candidates[0]
        binding = StoredBinding(app_id, identity, access, time.time())
        bindings = self._load_bindings()
        bindings[app_id] = binding.to_dict()
        self._save_bindings(bindings)
        return binding

    def bound(self, app_id: str, *, control: bool = False) -> StoredBinding:
        """Return the binding with a *re-verified, geometry-refreshed* identity."""

        if not isinstance(app_id, str) or _APP_ID.fullmatch(app_id) is None:
            raise InvalidActionError("app_id must be a safe lowercase identifier")
        bindings = self._load_bindings()
        raw = bindings.get(app_id)
        if not isinstance(raw, dict):
            raise DesktopSecurityError(f"desktop app {app_id!r} is not attached in this session")
        access = str(raw.get("access", "observe"))
        if control and access != "control":
            raise DesktopSecurityError(f"desktop app {app_id!r} is observation-only")
        pinned = WindowIdentity.from_dict(dict(raw.get("identity") or {}))
        try:
            identity = refresh_identity(self.probe, pinned)
        except TargetLostError:
            bindings.pop(app_id, None)
            self._save_bindings(bindings)
            raise
        return StoredBinding(app_id, identity, access, float(raw.get("attached_at", 0.0)))

    def detach(self, app_id: str) -> bool:
        bindings = self._load_bindings()
        existed = bindings.pop(app_id, None) is not None
        if existed:
            self._save_bindings(bindings)
        return existed

    def list_bindings(self) -> list[dict[str, Any]]:
        rows = []
        for app_id, raw in sorted(self._load_bindings().items()):
            if isinstance(raw, dict):
                rows.append(
                    {
                        "app_id": app_id,
                        "access": raw.get("access"),
                        "window": WindowIdentity.from_dict(
                            dict(raw.get("identity") or {})
                        ).public(),
                    }
                )
        return rows

    # -- capture history -------------------------------------------------------
    def capture_history(self) -> dict[str, Any]:
        raw = _read_json(self._history_path, {})
        return raw if isinstance(raw, dict) else {}

    def save_capture_history(self, history: dict[str, Any]) -> None:
        _atomic_write_json(self._history_path, history)

    # -- artifacts ---------------------------------------------------------------
    def put_artifact(self, png: bytes, name: str) -> dict[str, Any]:
        if not isinstance(png, bytes) or not png:
            raise InvalidActionError("artifact payload must be non-empty bytes")
        if len(png) > _MAX_ARTIFACT_BYTES:
            raise InvalidActionError("artifact exceeds the size cap")
        artifact_id = f"cu_{secrets.token_hex(12)}.png"
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self._artifacts_dir / artifact_id
        path.write_bytes(png)
        return {
            "artifact_id": artifact_id,
            "name": str(name)[:120],
            "mime": "image/png",
            "size": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "path": str(path),
            "created_at": time.time(),
            "session_id": self.session_id,
        }


__all__ = ["DesktopSession", "StoredBinding"]
