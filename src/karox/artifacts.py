"""Session-scoped artifact storage for hosted bridge tools.

Browser screenshots and other binary evidence a hosted client (ChatGPT, Claude)
needs to *see* cannot live inside the repository: :func:`CoreRuntime.safe_path`
blocks the ``.karox`` directory and writing PNGs into a user's source tree is not
what a verification run is for.  Artifacts are kept under the runtime dir, one
session per directory, and an artifact created by one KaroX session is never
reachable from another -- there is no path-by-name escape, only an opaque
``artifact_id`` validated against the owning session's store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import runtime_dir

_ARTIFACT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
# The image MIME types a hosted bridge tool is allowed to hand back to a client.
# Anything else is stored but never returned as image content.
_IMAGE_MIME = frozenset({"image/png"})
_DEFAULT_MIME = "application/octet-stream"
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024  # 25 MiB; a full-page PNG is well under this.


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    name: str
    mime: str
    size: int
    sha256: str
    created_at: str
    expires_at: str | None = None
    persistence_policy: str = "session"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "persistence_policy": self.persistence_policy,
        }


class ArtifactStore:
    """One session's artifact directory, reachable only by that session."""

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("artifact store requires a session id")
        self.session_id = session_id
        base = Path(root) if root is not None else runtime_dir() / "vnext" / "artifacts"
        self.root = (base / session_id).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, artifact_id: str) -> Path:
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("artifact id is malformed")
        return self.root / f"{artifact_id}.json"

    def _blob_path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.bin"

    def put(
        self,
        data: bytes,
        *,
        name: str,
        mime: str = _DEFAULT_MIME,
    ) -> ArtifactRecord:
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("artifact data must be bytes")
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds the maximum allowed size")
        if not isinstance(name, str) or not name or len(name) > 256:
            raise ValueError("artifact name must be a 1-256 character string")
        if not isinstance(mime, str) or not mime or len(mime) > 128:
            mime = _DEFAULT_MIME
        digest = hashlib.sha256(bytes(data)).hexdigest()
        # The id is content-addressed plus a short name stamp, so a second put of
        # the same bytes under the same name returns the same artifact instead of
        # silently writing a duplicate.
        seed = f"{name}\0{mime}\0{digest}".encode("utf-8")
        artifact_id = f"art-{hashlib.sha256(seed).hexdigest()[:20]}"
        meta_path = self._meta_path(artifact_id)
        blob_path = self._blob_path(artifact_id)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        # Write the blob first; a meta file pointing at a missing blob is worse
        # than an orphaned blob that nothing references.
        tmp_blob = tempfile.mkstemp(prefix=f".{artifact_id}.", dir=self.root)
        try:
            fd, tmp_name = tmp_blob
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(bytes(data))
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            os.replace(tmp_name, blob_path)
        except OSError:
            raise
        record = ArtifactRecord(
            artifact_id=artifact_id,
            name=name,
            mime=mime,
            size=len(data),
            sha256=digest,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        meta_tmp = tempfile.mkstemp(prefix=f".{artifact_id}.meta.", dir=self.root)
        try:
            fd, tmp_name = meta_tmp
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(record.to_dict(), handle, ensure_ascii=False, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, meta_path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            raise
        return record

    def _read_meta(self, artifact_id: str) -> ArtifactRecord:
        try:
            payload = json.loads(self._meta_path(artifact_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"artifact does not exist: {artifact_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise OSError("artifact metadata is unreadable") from exc
        return ArtifactRecord(
            artifact_id=str(payload["artifact_id"]),
            name=str(payload["name"]),
            mime=str(payload["mime"]),
            size=int(payload["size"]),
            sha256=str(payload["sha256"]),
            created_at=str(payload.get("created_at") or "historical"),
            expires_at=(
                str(payload["expires_at"])
                if isinstance(payload.get("expires_at"), str)
                else None
            ),
            persistence_policy=str(payload.get("persistence_policy") or "session"),
        )

    def read(self, artifact_id: str) -> tuple[bytes, ArtifactRecord]:
        record = self._read_meta(artifact_id)
        try:
            data = self._blob_path(artifact_id).read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"artifact blob is missing: {artifact_id}") from exc
        return data, record

    def read_image(self, artifact_id: str) -> tuple[bytes, str]:
        """Return ``(png_bytes, mime)`` for an image artifact owned by this session.

        Raises ``ValueError`` for non-image artifacts so a caller can never coax a
        ``karox.artifact.read_image`` response into shipping arbitrary file bytes
        out of the runtime directory.
        """
        data, record = self.read(artifact_id)
        if record.mime not in _IMAGE_MIME:
            raise ValueError("artifact is not an image")
        return data, record.mime

    def read_selection(
        self,
        artifact_id: str,
        selector: dict[str, Any],
        *,
        max_output_bytes: int = 64 * 1024,
    ) -> dict[str, Any]:
        """Read a bounded text/JSON selection without returning the full artifact."""
        if not isinstance(selector, dict):
            raise ValueError("artifact selector must be an object")
        if not 1024 <= max_output_bytes <= 1024 * 1024:
            raise ValueError("artifact selection limit must be 1024-1048576 bytes")
        data, record = self.read(artifact_id)
        if record.mime not in {
            "application/json",
            "application/x-ndjson",
            "text/plain",
            "text/markdown",
        }:
            raise ValueError("artifact does not support text selection")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("artifact text is not valid UTF-8") from exc
        kind = selector.get("kind", "line_range")
        diagnostics: dict[str, Any]
        if kind == "line_range":
            lines = text.splitlines()
            start = selector.get("start", 1)
            count = selector.get("count", 200)
            if not isinstance(start, int) or isinstance(start, bool) or start < 1:
                raise ValueError("line_range start must be a positive integer")
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 5000:
                raise ValueError("line_range count must be between 1 and 5000")
            selected: Any = "\n".join(lines[start - 1 : start - 1 + count])
            diagnostics = {"start": start, "count": count, "total_lines": len(lines)}
        elif kind == "tail":
            lines = text.splitlines()
            count = selector.get("count", 200)
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 5000:
                raise ValueError("tail count must be between 1 and 5000")
            selected = "\n".join(lines[-count:])
            diagnostics = {"count": count, "total_lines": len(lines)}
        elif kind in {"regex", "first_failure"}:
            pattern = selector.get("pattern") if kind == "regex" else r"(?i)(fail(?:ed|ure)?|error|traceback|exception)"
            if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
                raise ValueError("regex pattern must be a 1-500 character string")
            try:
                expression = re.compile(pattern)
            except re.error as exc:
                raise ValueError("artifact regex is invalid") from exc
            max_matches = 1 if kind == "first_failure" else selector.get("max_matches", 50)
            if not isinstance(max_matches, int) or isinstance(max_matches, bool) or not 1 <= max_matches <= 500:
                raise ValueError("max_matches must be between 1 and 500")
            matches = [
                {"line": index, "text": line}
                for index, line in enumerate(text.splitlines(), start=1)
                if expression.search(line)
            ][:max_matches]
            selected = matches
            diagnostics = {"match_count": len(matches), "max_matches": max_matches}
        elif kind in {"json_path", "section"}:
            try:
                value: Any = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("artifact is not valid JSON") from exc
            path = selector.get("path") or selector.get("name")
            if not isinstance(path, str) or not path or len(path) > 500:
                raise ValueError("JSON path must be a 1-500 character string")
            for part in path.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                    value = value[int(part)]
                else:
                    raise KeyError(f"artifact JSON path does not exist: {path}")
            selected = value
            diagnostics = {"path": path}
        else:
            raise ValueError("unsupported artifact selector kind")
        encoded = json.dumps(selected, ensure_ascii=False, default=str).encode("utf-8")
        truncated = len(encoded) > max_output_bytes
        if truncated:
            selected = encoded[:max_output_bytes].decode("utf-8", errors="ignore")
        return {
            "artifact_id": record.artifact_id,
            "selector": kind,
            "content": selected,
            "truncated": truncated,
            "selected_size": min(len(encoded), max_output_bytes),
            "total_size": record.size,
            "content_hash": record.sha256,
            "diagnostics": diagnostics,
        }

    def list(self) -> tuple[ArtifactRecord, ...]:
        records: list[ArtifactRecord] = []
        for meta_path in sorted(self.root.glob("*.json")):
            try:
                records.append(self._read_meta(meta_path.stem))
            except (OSError, ValueError, KeyError):
                continue
        return tuple(records)

    def exists(self, artifact_id: str) -> bool:
        try:
            return self._meta_path(artifact_id).is_file()
        except ValueError:
            return False

    @staticmethod
    def is_image_mime(mime: str) -> bool:
        return mime in _IMAGE_MIME
