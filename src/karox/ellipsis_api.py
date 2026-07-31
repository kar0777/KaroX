"""Typed, repository-free Ellipsis interactive-session transport.

Ellipsis' public documentation does not publish the enterprise REST schema.  This
adapter therefore keeps every path in one versioned contract and performs a
capability preflight before a live session is created.  The contract can be
replaced through ``ELLIPSIS_API_CONTRACT_JSON`` without changing KaroX's local
execution boundary.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

import httpx


class EllipsisError(RuntimeError):
    """A safe-to-display Ellipsis transport failure."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EllipsisContractError(EllipsisError):
    """The configured API does not satisfy the interactive-session contract."""


@dataclass(frozen=True)
class EllipsisApiContract:
    """All private API assumptions isolated in one replaceable value."""

    version: str = "interactive-v1"
    capabilities_path: str = "/v1/capabilities"
    create_path: str = "/v1/interactive-sessions"
    variables_path: str = "/v1/interactive-sessions/{session_id}/sandbox-variables"
    start_path: str = "/v1/interactive-sessions/{session_id}/start"
    message_path: str = "/v1/interactive-sessions/{session_id}/messages"
    status_path: str = "/v1/interactive-sessions/{session_id}"
    events_path: str = "/v1/interactive-sessions/{session_id}/events"
    stop_path: str = "/v1/interactive-sessions/{session_id}/stop"
    repositories_mode: str = "empty"  # empty or omit

    def __post_init__(self) -> None:
        if self.repositories_mode not in {"empty", "omit"}:
            raise ValueError("repositories_mode must be empty or omit")
        for name in (
            "capabilities_path",
            "create_path",
            "variables_path",
            "start_path",
            "message_path",
            "status_path",
            "events_path",
            "stop_path",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.startswith("/"):
                raise ValueError(f"{name} must be an absolute API path")

    @classmethod
    def from_environment(cls) -> "EllipsisApiContract":
        raw = os.environ.get("ELLIPSIS_API_CONTRACT_JSON", "").strip()
        if not raw:
            return cls()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EllipsisContractError(
                "ELLIPSIS_API_CONTRACT_JSON must be a JSON object"
            ) from exc
        if not isinstance(payload, dict):
            raise EllipsisContractError(
                "ELLIPSIS_API_CONTRACT_JSON must be a JSON object"
            )
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = set(payload).difference(allowed)
        if unknown:
            raise EllipsisContractError(
                "unknown Ellipsis contract fields: " + ", ".join(sorted(unknown))
            )
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise EllipsisContractError(str(exc)) from exc

    def path(self, template: str, session_id: str) -> str:
        if not session_id or any(char in session_id for char in "\r\n/?#"):
            raise EllipsisContractError("Ellipsis session ID is malformed")
        return template.format(session_id=session_id)


@dataclass(frozen=True)
class EllipsisDraft:
    session_id: str
    raw_status: str = "draft"


@dataclass(frozen=True)
class EllipsisEvent:
    cursor: str
    kind: str
    message: Optional[str] = None
    tool: Optional[str] = None
    status: Optional[str] = None
    cost: Optional[float] = None
    currency: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EllipsisStatus:
    session_id: str
    status: str
    cost: Optional[float]
    currency: Optional[str]
    elapsed_seconds: Optional[float]
    raw: Mapping[str, Any] = field(default_factory=dict)


def _safe_json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_error_code(response: httpx.Response) -> str:
    payload = _safe_json(response)
    for key in ("error_code", "code", "type"):
        value = payload.get(key)
        if isinstance(value, str) and 0 < len(value) <= 80:
            return value
    return "remote_error"


def repository_free_create_payload(
    *,
    model: str,
    system_instruction: str,
    budget: float,
    currency: str,
    repositories_mode: str,
    client_install: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    if model != "claude-opus-5":
        raise ValueError("Ellipsis integration currently requires claude-opus-5")
    if not system_instruction.strip():
        raise ValueError("system instruction must not be empty")
    if budget <= 0:
        raise ValueError("budget must be positive")
    sandbox: dict[str, Any] = {}
    if repositories_mode == "empty":
        sandbox["repositories"] = []
    elif repositories_mode != "omit":
        raise ValueError("repositories_mode must be empty or omit")
    if client_install:
        sandbox["setup"] = dict(client_install)
    payload = {
        "mode": "interactive",
        "model": model,
        "system_instruction": system_instruction,
        "budget": {"limit": float(budget), "currency": currency},
        "sandbox": sandbox,
        "metadata": {
            "integration": "karox",
            "workspace": "remote-tools-only",
        },
    }
    assert_repository_free(payload)
    return payload


def assert_repository_free(payload: Mapping[str, Any]) -> None:
    """Reject any project repository metadata before it leaves KaroX."""

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).lower()
                next_path = (*path, lowered)
                if lowered == "repository":
                    raise EllipsisContractError(
                        "Ellipsis payload must not contain repository metadata"
                    )
                if lowered == "repositories":
                    if next_path != ("sandbox", "repositories") or item != []:
                        raise EllipsisContractError(
                            "Ellipsis sandbox repositories must be empty or omitted"
                        )
                walk(item, next_path)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, path)

    walk(payload, ())


class EllipsisClient:
    """Small synchronous client for the private interactive-session contract."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        contract: Optional[EllipsisApiContract] = None,
        timeout_seconds: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            if transport is None:
                raise ValueError("Ellipsis API base URL must use HTTPS")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("ELLIPSIS_API_TOKEN is required")
        if any(char in token for char in "\r\n\0"):
            raise ValueError("ELLIPSIS_API_TOKEN is malformed")
        self.contract = contract or EllipsisApiContract.from_environment()
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "KaroX/ellipsis-adapter",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EllipsisClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        expected: Iterable[int] = (200,),
    ) -> Mapping[str, Any]:
        try:
            response = self._client.request(
                method, path, json=json_body, params=params
            )
        except httpx.TimeoutException as exc:
            raise EllipsisError("Ellipsis request timed out") from exc
        except httpx.HTTPError as exc:
            raise EllipsisError("Ellipsis transport failed") from exc
        if response.status_code not in set(expected):
            code = _safe_error_code(response)
            raise EllipsisError(
                f"Ellipsis request failed with HTTP {response.status_code} ({code})",
                status_code=response.status_code,
            )
        payload = _safe_json(response)
        if not payload and response.content:
            raise EllipsisContractError("Ellipsis returned a non-object response")
        return payload

    def preflight(self) -> Mapping[str, Any]:
        payload = self._request(
            "GET", self.contract.capabilities_path, expected=(200,)
        )
        modes = payload.get("session_modes", payload.get("modes", []))
        if isinstance(modes, list) and modes and "interactive" not in modes:
            raise EllipsisContractError(
                "configured Ellipsis API does not advertise interactive sessions"
            )
        variable_mode = payload.get("sandbox_variables")
        if variable_mode not in (None, True, "write_only"):
            raise EllipsisContractError(
                "configured Ellipsis API does not advertise write-only sandbox variables"
            )
        return payload

    def create_draft(
        self,
        *,
        model: str,
        system_instruction: str,
        budget: float,
        currency: str = "USD",
        client_install: Optional[Mapping[str, Any]] = None,
    ) -> EllipsisDraft:
        payload = repository_free_create_payload(
            model=model,
            system_instruction=system_instruction,
            budget=budget,
            currency=currency,
            repositories_mode=self.contract.repositories_mode,
            client_install=client_install,
        )
        try:
            response = self._request(
                "POST", self.contract.create_path, json_body=payload, expected=(200, 201)
            )
        except EllipsisError as exc:
            # Some enterprise schemas reject an explicit empty list. Retrying
            # without the field remains repository-free and follows Ellipsis'
            # own schema rather than substituting the current Git remote. Only a
            # schema-validation response is eligible; auth/server failures are
            # never replayed as a different request.
            if (
                self.contract.repositories_mode != "empty"
                or exc.status_code not in {400, 422}
            ):
                raise
            omitted = dict(payload)
            omitted["sandbox"] = dict(payload["sandbox"])
            omitted["sandbox"].pop("repositories", None)
            assert_repository_free(omitted)
            response = self._request(
                "POST", self.contract.create_path, json_body=omitted, expected=(200, 201)
            )
        session_id = response.get("session_id", response.get("id"))
        if not isinstance(session_id, str) or not session_id:
            raise EllipsisContractError("Ellipsis draft response has no session ID")
        return EllipsisDraft(session_id, str(response.get("status") or "draft"))

    def set_write_only_variables(
        self, session_id: str, variables: Mapping[str, str]
    ) -> None:
        required = {
            "KAROX_REMOTE_URL",
            "KAROX_REMOTE_CREDENTIAL",
            "KAROX_SESSION_ID",
        }
        if set(variables) != required:
            raise ValueError(
                "Ellipsis sandbox variables must be exactly: "
                + ", ".join(sorted(required))
            )
        if any(
            not isinstance(value, str)
            or not value
            or any(char in value for char in "\r\n\0")
            for value in variables.values()
        ):
            raise ValueError("Ellipsis sandbox variable value is malformed")
        body = {
            "variables": [
                {"name": name, "value": value, "visibility": "write_only"}
                for name, value in sorted(variables.items())
            ]
        }
        self._request(
            "PUT",
            self.contract.path(self.contract.variables_path, session_id),
            json_body=body,
            expected=(200, 204),
        )

    def start(self, session_id: str, task: str) -> Mapping[str, Any]:
        if not task.strip():
            raise ValueError("task must not be empty")
        return self._request(
            "POST",
            self.contract.path(self.contract.start_path, session_id),
            json_body={"message": task},
            expected=(200, 202),
        )

    def send(self, session_id: str, message: str) -> Mapping[str, Any]:
        if not message.strip():
            raise ValueError("message must not be empty")
        return self._request(
            "POST",
            self.contract.path(self.contract.message_path, session_id),
            json_body={"message": message},
            expected=(200, 202),
        )

    def stop(self, session_id: str) -> Mapping[str, Any]:
        return self._request(
            "POST",
            self.contract.path(self.contract.stop_path, session_id),
            json_body={},
            expected=(200, 202, 204),
        )

    def status(self, session_id: str) -> EllipsisStatus:
        payload = self._request(
            "GET",
            self.contract.path(self.contract.status_path, session_id),
            expected=(200,),
        )
        usage = payload.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        cost = usage.get("cost", payload.get("cost"))
        elapsed = payload.get("elapsed_seconds")
        return EllipsisStatus(
            session_id=session_id,
            status=str(payload.get("status") or "unknown"),
            cost=float(cost) if isinstance(cost, (int, float)) else None,
            currency=(
                str(usage.get("currency", payload.get("currency")))
                if usage.get("currency", payload.get("currency")) is not None
                else None
            ),
            elapsed_seconds=(
                float(elapsed) if isinstance(elapsed, (int, float)) else None
            ),
            raw=payload,
        )

    def events(
        self, session_id: str, *, after: Optional[str] = None
    ) -> tuple[EllipsisEvent, ...]:
        params = {"after": after} if after else None
        payload = self._request(
            "GET",
            self.contract.path(self.contract.events_path, session_id),
            params=params,
            expected=(200,),
        )
        raw_events = payload.get("events", [])
        if not isinstance(raw_events, list):
            raise EllipsisContractError("Ellipsis events response is malformed")
        result: list[EllipsisEvent] = []
        for index, raw in enumerate(raw_events):
            if not isinstance(raw, Mapping):
                continue
            cursor = raw.get("cursor", raw.get("id", f"{time.time_ns()}-{index}"))
            usage = raw.get("usage")
            usage = usage if isinstance(usage, Mapping) else {}
            cost = usage.get("cost", raw.get("cost"))
            result.append(
                EllipsisEvent(
                    cursor=str(cursor),
                    kind=str(raw.get("kind", raw.get("type", "event"))),
                    message=(
                        str(raw["message"]) if isinstance(raw.get("message"), str) else None
                    ),
                    tool=str(raw["tool"]) if isinstance(raw.get("tool"), str) else None,
                    status=(
                        str(raw["status"]) if isinstance(raw.get("status"), str) else None
                    ),
                    cost=float(cost) if isinstance(cost, (int, float)) else None,
                    currency=(
                        str(usage.get("currency", raw.get("currency")))
                        if usage.get("currency", raw.get("currency")) is not None
                        else None
                    ),
                    payload=raw,
                )
            )
        return tuple(result)
