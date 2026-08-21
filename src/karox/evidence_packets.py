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
import re
from dataclasses import dataclass, field, replace
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


_COUNT_PATTERN = re.compile(r"(\d+)\s+(passed|failed|errors?)\b", re.IGNORECASE)


def _parse_test_counts(text: str) -> Optional[dict[str, int]]:
    """Parse pytest-style counters from a summary line; never invents them."""

    totals: dict[str, int] = {}
    for number, label in _COUNT_PATTERN.findall(text or ""):
        key = "errors" if label.lower().startswith("error") else label.lower()
        totals[key] = totals.get(key, 0) + int(number)
    if not totals:
        return None
    return {
        "passed": totals.get("passed", 0),
        "failed": totals.get("failed", 0),
        "errors": totals.get("errors", 0),
    }


def _safe_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def checks_job_packet(
    *,
    status: str,
    exit_code: Optional[int],
    summary: Optional[str],
    first_failure: Optional[str],
    duration_seconds: Optional[float] = None,
    artifact: Optional[ArtifactRef] = None,
    error_code: Optional[str] = None,
) -> EvidencePacket:
    """Typed packet for one finished managed check job.

    Honest by construction: ``passed`` requires the job status ``passed``
    with a zero exit code, unknown counters are never invented, and the
    first failure line plus the raw-log artifact reference survive every
    fidelity level.
    """

    counts = _parse_test_counts(summary or "")
    failed_counts = counts is not None and (
        counts["failed"] > 0 or counts["errors"] > 0
    )
    succeeded = status == "passed" and exit_code == 0 and not failed_counts
    critical: list[str] = []
    if not succeeded:
        critical.append(f"status={status} exit_code={exit_code}")
        if error_code:
            critical.append(f"error_code: {error_code}")
        if first_failure:
            critical.append(f"primary_failure: {first_failure[:500]}")
    metrics: dict[str, MetricValue] = {}
    if exit_code is not None:
        metrics["exit_code"] = exit_code
    if duration_seconds is not None:
        metrics["duration_seconds"] = round(float(duration_seconds), 3)
    kind = PacketKind.BUILD
    if counts is not None:
        kind = PacketKind.TESTS
        metrics.update(counts)
    line = summary or ("check passed" if succeeded else "check failed")
    return EvidencePacket(
        kind=kind,
        status="passed" if succeeded else "failed",
        summary=line[:500],
        critical=tuple(critical),
        metrics=metrics,
        artifact=artifact,
    )


def packet_from_plan_result(
    action: str,
    result: Mapping[str, object],
    *,
    success: bool,
) -> Optional[EvidencePacket]:
    """Typed packet for one plan operation result.

    Returns ``None`` for actions without a typed form yet so the caller
    falls back to the lossless summary policy. ``success`` is the
    executor's own verdict: a failing operation can never render as a
    passing packet, whatever the raw payload claims.
    """

    merged: dict[str, object] = dict(result)
    data = result.get("data")
    if isinstance(data, Mapping):
        for key, value in data.items():
            merged.setdefault(str(key), value)
    exit_code = _safe_int(merged.get("exit_code"))
    error_code = merged.get("error_code")
    packet: Optional[EvidencePacket] = None
    if action == "checks":
        summary_value = merged.get("summary")
        counts: Optional[dict[str, int]] = None
        for candidate in (summary_value, merged.get("stdout"), merged.get("stderr")):
            if isinstance(candidate, str):
                counts = _parse_test_counts(candidate)
                if counts is not None:
                    break
        failed_counts = counts is not None and (
            counts["failed"] > 0 or counts["errors"] > 0
        )
        run_success = (
            success
            and not bool(merged.get("timed_out"))
            and (exit_code is None or exit_code == 0)
            and not failed_counts
        )
        critical: list[str] = []
        if bool(merged.get("timed_out")):
            critical.append("timed_out=true")
        if not run_success:
            critical.append(f"exit_code={exit_code}")
            if isinstance(error_code, str) and error_code:
                critical.append(f"error_code: {error_code}")
            failure = merged.get("first_failure")
            if isinstance(failure, str) and failure:
                critical.append(f"primary_failure: {failure[:500]}")
        metrics: dict[str, MetricValue] = {}
        if exit_code is not None:
            metrics["exit_code"] = exit_code
        kind = PacketKind.BUILD
        if counts is not None:
            kind = PacketKind.TESTS
            metrics.update(counts)
        line = (
            summary_value
            if isinstance(summary_value, str) and summary_value
            else ("checks passed" if run_success else "checks failed")
        )
        packet = EvidencePacket(
            kind=kind,
            status="passed" if run_success else "failed",
            summary=str(line)[:500],
            critical=tuple(critical),
            metrics=metrics,
        )
    elif action == "search":
        matches = merged.get("matches")
        files: list[str] = []
        truncated = bool(merged.get("truncated"))
        if isinstance(matches, (list, tuple)):
            for item in matches:
                if isinstance(item, Mapping):
                    path = item.get("path")
                    if isinstance(path, str) and path not in files:
                        if len(files) >= 20:
                            truncated = True
                            break
                        files.append(path)
        packet = search_packet(
            query=str(merged.get("query", ""))[:200],
            match_count=_safe_int(merged.get("match_count")) or 0,
            files=tuple(files),
            truncated=truncated,
        )
    elif action == "browser":
        title = merged.get("title") or merged.get("url") or "browser action"
        packet = EvidencePacket(
            kind=PacketKind.BROWSER,
            status="ok",
            summary=str(title)[:200],
        )
    if packet is None:
        return None
    if not success and not packet.failed:
        notes = ["operation failed"]
        if isinstance(error_code, str) and error_code:
            notes.append(f"error_code: {error_code}")
        packet = replace(
            packet,
            status="failed",
            critical=packet.critical + tuple(notes),
        )
    return packet


__all__ = [
    "ArtifactRef",
    "EvidencePacket",
    "Fidelity",
    "MetricValue",
    "PacketKind",
    "build_packet",
    "checks_job_packet",
    "git_packet",
    "packet_from_plan_result",
    "search_packet",
    "tests_packet",
]
