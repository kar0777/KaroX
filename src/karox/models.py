"""Shared, provider-independent runtime models."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class OriginKind(str, Enum):
    USER = "user"
    NATIVE_AGENT = "native_agent"
    HOSTED_CLIENT = "hosted_client"
    SKILL = "skill"
    PROXIED_MCP = "proxied_mcp"
    PACK = "pack"


class AccessProfile(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    ELEVATED = "elevated"


class Capability(str, Enum):
    REPO_READ = "repo.read"
    REPO_WRITE = "repo.write"
    PROCESS_RUN = "process.run"
    CHECKS_RUN = "checks.run"
    GIT_READ = "git.read"
    GIT_COMMIT = "git.commit"
    GIT_PUSH = "git.push"
    BROWSER_READ = "browser.read"
    BROWSER_INPUT = "browser.input"
    DESKTOP_INPUT = "desktop.input"
    NETWORK = "network"
    MCP_CALL = "mcp.call"
    PACKAGE_PUBLISH = "package.publish"
    AUTH_COMMAND = "auth.command"


@dataclass(frozen=True)
class Origin:
    kind: OriginKind
    identity: str
    parent: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.identity.strip() or len(self.identity) > 200:
            raise ValueError("origin identity must contain 1-200 characters")
        if self.parent is not None and (
            not isinstance(self.parent, str)
            or not self.parent.strip()
            or len(self.parent) > 200
        ):
            raise ValueError("origin parent must contain 1-200 characters")

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.identity}"


@dataclass(frozen=True)
class CoreCommand:
    name: str
    arguments: Dict[str, Any]
    session_id: str
    origin: Origin
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    idempotency_key: Optional[str] = None
    deadline_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("command name must be a non-empty string")
        if not isinstance(self.arguments, dict):
            raise ValueError("command arguments must be an object")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session ID must be a non-empty string")
        if not isinstance(self.correlation_id, str) or not self.correlation_id.strip():
            raise ValueError("correlation ID must be a non-empty string")
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str)
            or not self.idempotency_key.strip()
            or len(self.idempotency_key) > 200
        ):
            raise ValueError("idempotency key must contain 1-200 characters")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not 0.1 <= float(self.deadline_seconds) <= 3600.0
        ):
            raise ValueError("deadline must be between 0.1 and 3600 seconds")

    def input_digest(self) -> str:
        canonical = json.dumps(
            {"name": self.name, "arguments": self.arguments},
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class EvidenceRecord:
    kind: str
    summary: str
    command: Optional[List[str]] = None
    exit_code: Optional[int] = None
    artifact_sha256: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    evidence_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("evidence kind must be a non-empty string")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("evidence summary must be a non-empty string")
        if self.command is not None and not (
            isinstance(self.command, list)
            and all(isinstance(item, str) for item in self.command)
        ):
            raise ValueError("evidence command must be a list of strings")
        if not isinstance(self.metadata, dict):
            raise ValueError("evidence metadata must be an object")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "EvidenceRecord":
        if not isinstance(value, dict):
            raise ValueError("evidence must be an object")
        allowed = {
            "kind",
            "summary",
            "command",
            "exit_code",
            "artifact_sha256",
            "metadata",
            "evidence_id",
        }
        unknown = set(value).difference(allowed)
        if unknown:
            raise ValueError(f"unknown evidence fields: {sorted(unknown)}")
        missing = {"kind", "summary"}.difference(value)
        if missing:
            raise ValueError(f"missing evidence fields: {sorted(missing)}")
        return cls(**value)


@dataclass
class CoreResult:
    ok: bool
    command: str
    data: Dict[str, Any]
    correlation_id: str
    mutation: bool = False
    evidence: List[EvidenceRecord] = field(default_factory=list)
    idempotent_replay: bool = False

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return value

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CoreResult":
        if not isinstance(value, dict):
            raise ValueError("Core result must be an object")
        allowed = {
            "ok",
            "command",
            "data",
            "correlation_id",
            "mutation",
            "evidence",
            "idempotent_replay",
        }
        unknown = set(value).difference(allowed)
        if unknown:
            raise ValueError(f"unknown Core result fields: {sorted(unknown)}")
        missing = {"ok", "command", "data", "correlation_id"}.difference(value)
        if missing:
            raise ValueError(f"missing Core result fields: {sorted(missing)}")
        payload = dict(value)
        if not isinstance(payload["ok"], bool):
            raise ValueError("Core result ok must be boolean")
        if not isinstance(payload["command"], str) or not payload["command"]:
            raise ValueError("Core result command must be a non-empty string")
        if not isinstance(payload["data"], dict):
            raise ValueError("Core result data must be an object")
        if not isinstance(payload["correlation_id"], str) or not payload["correlation_id"]:
            raise ValueError("Core result correlation_id must be a non-empty string")
        if not isinstance(payload.get("evidence", []), list):
            raise ValueError("Core result evidence must be a list")
        payload["evidence"] = [
            EvidenceRecord.from_dict(item) for item in payload.get("evidence", [])
        ]
        return cls(**payload)


def repository_fingerprint(path: Path) -> str:
    """Return a stable local identity without reading repository contents."""
    root = path.resolve(strict=True)
    git_marker = root / ".git"
    marker = ""
    if git_marker.exists():
        metadata = git_marker.stat()
        marker = f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_mode}"
    if git_marker.is_file():
        marker += "\n" + git_marker.read_text(
            encoding="utf-8", errors="replace"
        )[:1000]
    marker = os.path.normcase(marker)
    payload = f"{root}\n{marker}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()
