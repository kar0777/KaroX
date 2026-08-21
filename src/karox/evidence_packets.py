"""Typed compact result forms: Evidence Packets.

A packet is the model-facing form of one expensive operation result. It
carries a one-line summary, the critical evidence (failures, errors,
refusals -- never hidden at any fidelity), a small metrics mapping, and a
reference to the raw artifact when one exists. Elision is lossless by
construction: the packet never claims to replace raw output, it points at
it.

Fidelity levels (adaptive detail, section 14 of the Part 2 mandate):

``summary``
    Status, one-line summary, critical evidence, artifact reference.
``evidence``
    ``summary`` plus the metrics mapping and related files.
``full``
    Everything the packet holds, including the detail lines.
``auto``
    ``evidence`` for failures, ``summary`` for successes: enough to reason
    correctly by default, expandable on demand.

Two invariants hold at every fidelity: critical evidence always renders,
and the artifact reference always renders when the packet has one. Byte
accounting is honest -- ``bytes_avoided`` exists only when the raw size is
known; unknown stays ``None`` and is never invented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple, Union

MetricValue = Union[int, float, str, bool]


class PacketKind(str, Enum):
    SEARCH = "search"
    INSPECTION = "inspection"
    TESTS = "tests"
    GIT = "git"
    BUILD = "build"
    BROWSER = "browser"
    PROVIDER_PROBE = "provider_probe"


class Fidelity(str, Enum):
    AUTO = "auto"
    SUMMARY = "summary"
    EVIDENCE = "evidence"
    FULL = "full"


@dataclass(frozen=True)
class ArtifactRef:
    """Pointer to raw output that stays queryable after elision."""

    artifact_id: str
    sha256: Optional[str] = None
    raw_bytes: Optional[int] = None


@dataclass(frozen=True)
class EvidencePacket:
    """One typed, compact, honest result form.

    ``critical`` holds failure and error lines that must survive every
    fidelity level. ``detail`` holds bounded low-priority lines that only
    ``full`` renders. ``metrics`` values are scalars so the rendered form
    stays compact and deterministic.
    """

    kind: PacketKind
    status: str
    summary: str
    critical: Tuple[str, ...] = ()
    related_files: Tuple[str, ...] = ()
    metrics: Mapping[str, MetricValue] = field(default_factory=dict)
    detail: Tuple[str, ...] = ()
    artifact: Optional[ArtifactRef] = None

    @property
    def failed(self) -> bool:
        return self.status not in {"ok", "passed", "success"}

    def resolve_fidelity(self, fidelity: Fidelity) -> Fidelity:
        if fidelity is not Fidelity.AUTO:
            return fidelity
        return Fidelity.EVIDENCE if self.failed else Fidelity.SUMMARY

    def render(self, fidelity: Fidelity = Fidelity.AUTO) -> str:
        """Deterministic JSON at the requested fidelity.

        Critical evidence and the artifact reference render at every
        level; a low-detail mode may drop metrics and detail lines but
        never a failure.
        """

        level = self.resolve_fidelity(fidelity)
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "status": self.status,
            "summary": self.summary,
        }
        if self.critical:
            payload["critical"] = list(self.critical)
        if self.artifact is not None:
            reference: dict[str, object] = {
                "artifact_id": self.artifact.artifact_id
            }
            if self.artifact.sha256:
                reference["sha256"] = self.artifact.sha256
            if self.artifact.raw_bytes is not None:
                reference["raw_bytes"] = self.artifact.raw_bytes
            payload["artifact"] = reference
        if level in {Fidelity.EVIDENCE, Fidelity.FULL}:
            if self.metrics:
                payload["metrics"] = {
                    key: self.metrics[key] for key in sorted(self.metrics)
                }
            if self.related_files:
                payload["related_files"] = list(self.related_files)
        if level is Fidelity.FULL and self.detail:
            payload["detail"] = list(self.detail)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def bytes_avoided(self, fidelity: Fidelity = Fidelity.AUTO) -> Optional[int]:
        """Raw bytes minus rendered bytes; ``None`` when raw size is unknown."""

        if self.artifact is None or self.artifact.raw_bytes is None:
            return None
        rendered = len(self.render(fidelity).encode("utf-8"))
        return max(0, self.artifact.raw_bytes - rendered)


def tests_packet(
    *,
    exit_code: int,
    passed: int,
    failed: int,
    errors: int = 0,
    duration_seconds: Optional[float] = None,
    primary_failure: Optional[str] = None,
    related_files: Tuple[str, ...] = (),
    detail: Tuple[str, ...] = (),
    artifact: Optional[ArtifactRef] = None,
) -> EvidencePacket:
    status = "passed" if exit_code == 0 and failed == 0 and errors == 0 else "failed"
    summary = f"{passed} passed, {failed} failed, {errors} errors"
    if duration_seconds is not None:
        summary += f" in {duration_seconds:.1f}s"
    critical = []
    if status == "failed":
        critical.append(f"exit_code={exit_code}")
        if primary_failure:
            critical.append(f"primary_failure: {primary_failure}")
    metrics: dict[str, MetricValue] = {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "exit_code": exit_code,
    }
    if duration_seconds is not None:
        metrics["duration_seconds"] = round(duration_seconds, 3)
    return EvidencePacket(
        kind=PacketKind.TESTS,
        status=status,
        summary=summary,
        critical=tuple(critical),
        related_files=related_files,
        metrics=metrics,
        detail=detail,
        artifact=artifact,
    )


def search_packet(
    *,
    query: str,
    match_count: int,
    files: Tuple[str, ...],
    truncated: bool = False,
    detail: Tuple[str, ...] = (),
    artifact: Optional[ArtifactRef] = None,
) -> EvidencePacket:
    summary = f"{match_count} matches in {len(files)} files for {query!r}"
    critical = ("result truncated: raw artifact holds the rest",) if truncated else ()
    return EvidencePacket(
        kind=PacketKind.SEARCH,
        status="ok",
        summary=summary,
        critical=critical,
        related_files=files,
        metrics={"match_count": match_count, "file_count": len(files)},
        detail=detail,
        artifact=artifact,
    )


def git_packet(
    *,
    command: str,
    exit_code: int,
    summary: str,
    detail: Tuple[str, ...] = (),
    artifact: Optional[ArtifactRef] = None,
) -> EvidencePacket:
    status = "ok" if exit_code == 0 else "failed"
    critical = (f"git {command} exit_code={exit_code}",) if exit_code != 0 else ()
    return EvidencePacket(
        kind=PacketKind.GIT,
        status=status,
        summary=summary,
        critical=critical,
        metrics={"exit_code": exit_code},
        detail=detail,
        artifact=artifact,
    )


def build_packet(
    *,
    exit_code: int,
    summary: str,
    primary_error: Optional[str] = None,
    detail: Tuple[str, ...] = (),
    artifact: Optional[ArtifactRef] = None,
) -> EvidencePacket:
    status = "ok" if exit_code == 0 else "failed"
    critical = []
    if exit_code != 0:
        critical.append(f"exit_code={exit_code}")
        if primary_error:
            critical.append(f"primary_error: {primary_error}")
    return EvidencePacket(
        kind=PacketKind.BUILD,
        status=status,
        summary=summary,
        critical=tuple(critical),
        metrics={"exit_code": exit_code},
        detail=detail,
        artifact=artifact,
    )


__all__ = [
    "ArtifactRef",
    "EvidencePacket",
    "Fidelity",
    "MetricValue",
    "PacketKind",
    "build_packet",
    "git_packet",
    "search_packet",
    "tests_packet",
]
