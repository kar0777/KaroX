from __future__ import annotations

import json
from typing import Any

from karox.oauth_bridge import OAuthBridgeError, OAuthBridgeService


def test_cimd_is_opt_in_and_resolves_allowlisted_metadata() -> None:
    ordinary = OAuthBridgeService("https://karox.example", "test-approval-value")
    assert ordinary.authorization_server_metadata()["client_id_metadata_document_supported"] is False

    client_id = "https://www.notion.so/oauth/client-metadata.json"
    redirect_uri = "https://www.notion.so/external-auth/callback"
    payload = {
        "client_id": client_id,
        "client_name": "Notion",
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    seen: dict[str, Any] = {}

    class Response:
        status_code = 200
        content = json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json"}

    def get(url: str, **kwargs: Any) -> Response:
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    service = OAuthBridgeService(
        "https://karox.example",
        "test-approval-value",
        path="/notion-mcp",
        allowed_redirect_hosts=frozenset({"www.notion.so"}),
        allowed_client_metadata_hosts=frozenset({"www.notion.so"}),
        client_metadata_get=get,
    )
    assert service.authorization_server_metadata()["client_id_metadata_document_supported"] is True
    request_id, client = service.begin_authorization(
        {
            "response_type": ["code"],
            "client_id": [client_id],
            "redirect_uri": [redirect_uri],
            "state": ["state-notion"],
            "code_challenge": ["a" * 43],
            "code_challenge_method": ["S256"],
            "resource": [service.resource],
        }
    )
    assert request_id
    assert client.client_id == client_id
    assert seen["url"] == client_id
    assert seen["follow_redirects"] is False


def test_cimd_rejects_mismatched_client_id() -> None:
    client_id = "https://www.notion.so/oauth/client-metadata.json"

    class Response:
        status_code = 200
        content = json.dumps(
            {
                "client_id": "https://www.notion.so/oauth/other.json",
                "client_name": "Notion",
                "redirect_uris": ["https://www.notion.so/external-auth/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            }
        ).encode("utf-8")
        headers = {"content-type": "application/json"}

    service = OAuthBridgeService(
        "https://karox.example",
        "test-approval-value",
        path="/notion-mcp",
        allowed_redirect_hosts=frozenset({"www.notion.so"}),
        allowed_client_metadata_hosts=frozenset({"www.notion.so"}),
        client_metadata_get=lambda *args, **kwargs: Response(),
    )
    try:
        service.begin_authorization(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": ["https://www.notion.so/external-auth/callback"],
                "state": ["state-notion"],
                "code_challenge": ["a" * 43],
                "code_challenge_method": ["S256"],
                "resource": [service.resource],
            }
        )
    except OAuthBridgeError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("mismatched CIMD client_id was accepted")
