"""Provider-neutral quota observations for KaroX routing.

Quota is runtime telemetry, not provider configuration.  This store therefore
covers API, subscription, local and external endpoints without writing limits
into ProviderRegistry or pretending an unavailable quota is zero.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from .intelligence_pool import IntelligenceEndpoint, QuotaSnapshot
from .paths import runtime_dir


_SCHEMA_VERSION = 1


class QuotaBrainError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class QuotaObservation:
    endpoint_id: str
    quota: QuotaSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {"endpoint_id": self.endpoint_id, "quota": self.quota.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QuotaObservation":
        quota = value.get("quota")
        if not isinstance(quota, Mapping):
            raise ValueError("quota observation requires quota object")
        return cls(str(value["endpoint_id"]), QuotaSnapshot.from_dict(quota))


class QuotaBrain:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (path or (runtime_dir() / "vnext" / "quota-observations.json")).expanduser().resolve()

    def _load(self) -> dict[str, QuotaObservation]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuotaBrainError(f"cannot read quota observations: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise QuotaBrainError("quota observations have unsupported schema")
        raw = payload.get("observations")
        if not isinstance(raw, list):
            raise QuotaBrainError("quota observations must be an array")
        try:
            rows = [QuotaObservation.from_dict(item) for item in raw if isinstance(item, Mapping)]
        except (KeyError, TypeError, ValueError) as exc:
            raise QuotaBrainError(f"quota observations are invalid: {exc}") from exc
        return {item.endpoint_id: item for item in rows}

    def _save(self, rows: Mapping[str, QuotaObservation]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "observations": [rows[key].to_dict() for key in sorted(rows)],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def observe(
        self,
        endpoint_id: str,
        *,
        remaining_fraction: Optional[float] = None,
        remaining_units: Optional[float] = None,
        unit: Optional[str] = None,
        resets_at: Optional[float] = None,
        source: str,
        observed_at: Optional[float] = None,
    ) -> QuotaSnapshot:
        quota = QuotaSnapshot(
            remaining_fraction=remaining_fraction,
            remaining_units=remaining_units,
            unit=unit,
            resets_at=resets_at,
            observed_at=time.time() if observed_at is None else observed_at,
            source=source,
        )
        rows = self._load()
        rows[endpoint_id] = QuotaObservation(endpoint_id, quota)
        self._save(rows)
        return quota

    def get(self, endpoint_id: str) -> Optional[QuotaSnapshot]:
        row = self._load().get(endpoint_id)
        return None if row is None else row.quota

    def effective(self, endpoint: IntelligenceEndpoint, *, max_age_seconds: float = 86_400.0) -> QuotaSnapshot:
        observed = self.get(endpoint.endpoint_id)
        now = time.time()
        if observed is not None and observed.observed_at > 0 and now - observed.observed_at <= max_age_seconds:
            return observed
        return endpoint.quota

    def list(self) -> list[QuotaObservation]:
        rows = self._load()
        return [rows[key] for key in sorted(rows)]


__all__ = ["QuotaBrain", "QuotaBrainError", "QuotaObservation"]
