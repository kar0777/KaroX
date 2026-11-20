import asyncio
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from tests.test_mcp import TOKEN, make_manager

from karox6.mcp.oauth import OAuth
from karox6.mcp.protocol import McpServer
from karox6.mcp.transport import HttpMcp

REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18"},
}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def pkce():
    verifier = "v" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def serve(tmp_path, steps):
    manager, _ = make_manager(tmp_path, [])
    events: list[str] = []
    oauth = OAuth(tmp_path / "oauth.json", on_event=events.append)
    log: list[str] = []
    http = HttpMcp(McpServer(manager), token=TOKEN, oauth=oauth, access_log=log.append)

    async def go():
        listener = await http.start("127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        async with listener, httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            await steps(client, oauth)

    asyncio.run(go())
    return events, log


def test_chatgpt_style_sign_in_end_to_end(tmp_path):
    async def steps(client, oauth):
        # unauthenticated MCP call: 401 pointing at the resource metadata
        r = await client.post("/mcp", json=INIT)
        assert r.status_code == 401
        assert "resource_metadata=" in r.headers["www-authenticate"]
        # every discovery spelling the web clients probe answers
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
            "/mcp/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp",
            "/.well-known/openid-configuration",
            "/mcp/.well-known/openid-configuration",
        ):
            assert (await client.get(path)).status_code == 200, path
        meta = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert meta["code_challenge_methods_supported"] == ["S256"]
        # dynamic registration with the code grant only (as ChatGPT registers)
        reg = await client.post(
            "/oauth/register",
            json={
                "redirect_uris": [REDIRECT],
                "grant_types": ["authorization_code"],
                "client_name": "ChatGPT",
            },
        )
        assert reg.status_code == 201
        client_id = reg.json()["client_id"]
        verifier, challenge = pkce()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s1",
            }
        )
        page = await client.get(f"/oauth/authorize?{query}")
        assert page.status_code == 200
        assert "approval code" in page.text
        request_id = re.search(r"name=request_id value='([^']+)'", page.text).group(1)
        wrong = await client.post(
            "/oauth/authorize",
            content=urlencode({"request_id": request_id, "code": "NOPE"}),
            headers=FORM,
        )
        assert "Wrong code" in wrong.text
        typed = f"{oauth.pin[:4].lower()}-{oauth.pin[4:]}"  # case and dash do not matter
        ok = await client.post(
            "/oauth/authorize",
            content=urlencode({"request_id": request_id, "code": typed}),
            headers=FORM,
        )
        assert ok.status_code == 302
        back = parse_qs(urlsplit(ok.headers["location"]).query)
        assert back["state"] == ["s1"]
        bad = await client.post(
            "/oauth/token",
            content=urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": back["code"][0],
                    "redirect_uri": REDIRECT,
                    "client_id": client_id,
                    "code_verifier": "x" * 64,
                }
            ),
            headers=FORM,
        )
        assert bad.json()["error"] == "invalid_grant"  # wrong verifier burns the code
        # a fresh round with the right verifier
        page = await client.get(f"/oauth/authorize?{query}")
        request_id = re.search(r"name=request_id value='([^']+)'", page.text).group(1)
        ok = await client.post(
            "/oauth/authorize",
            content=urlencode({"request_id": request_id, "code": oauth.pin}),
            headers=FORM,
        )
        code = parse_qs(urlsplit(ok.headers["location"]).query)["code"][0]
        tok = (
            await client.post(
                "/oauth/token",
                content=urlencode(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": REDIRECT,
                        "client_id": client_id,
                        "code_verifier": verifier,
                    }
                ),
                headers=FORM,
            )
        ).json()
        bearer = {"Authorization": f"Bearer {tok['access_token']}"}
        r = await client.post("/mcp", json=INIT, headers=bearer)
        assert r.status_code == 200
        assert r.json()["result"]["serverInfo"]["name"] == "karox"
        # refresh rotates: the old refresh token stops working
        new = (
            await client.post(
                "/oauth/token",
                content=urlencode(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": tok["refresh_token"],
                        "client_id": client_id,
                    }
                ),
                headers=FORM,
            )
        ).json()
        assert new["access_token"] != tok["access_token"]
        again = await client.post(
            "/oauth/token",
            content=urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tok["refresh_token"],
                }
            ),
            headers=FORM,
        )
        assert again.json()["error"] == "invalid_grant"
        # a restarted server (new instance, same state file) still accepts the new token
        assert OAuth(oauth.state_path).valid_access(new["access_token"])
        # the token-in-the-URL access keeps working beside OAuth
        assert (await client.post(f"/mcp/{TOKEN}", json=INIT)).status_code == 200

    events, log = serve(tmp_path, steps)
    assert "sign-in approved" in events
    assert any(line.startswith("POST /mcp/<token> -> 200") for line in log)
    assert not any(TOKEN in line for line in log)
    saved = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert all(len(k) == 64 for k in saved["access"])  # only hashes are kept


def test_too_many_wrong_codes_end_the_sign_in(tmp_path):
    async def steps(client, oauth):
        reg = await client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
        _, challenge = pkce()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": reg.json()["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        page = await client.get(f"/oauth/authorize?{query}")
        request_id = re.search(r"name=request_id value='([^']+)'", page.text).group(1)
        for _ in range(5):
            last = await client.post(
                "/oauth/authorize",
                content=urlencode({"request_id": request_id, "code": "x"}),
                headers=FORM,
            )
        assert last.status_code == 403
        late = await client.post(
            "/oauth/authorize",
            content=urlencode({"request_id": request_id, "code": oauth.pin}),
            headers=FORM,
        )
        assert late.status_code == 400  # the request is gone, even with the right code
        evil = await client.get("/oauth/authorize?" + query.replace("chatgpt.com", "evil.example"))
        assert evil.status_code == 400

    serve(tmp_path, steps)


def test_sign_in_under_a_shared_path(tmp_path):
    """Published as https://host/karox6 (port 443 shared with another site): every link
    the server hands out carries the path, and requests work with or without it (whether
    or not the proxy strips it)."""
    manager, _ = make_manager(tmp_path, [])
    oauth = OAuth(tmp_path / "oauth.json")
    http = HttpMcp(McpServer(manager), token=TOKEN, oauth=oauth)
    http.mount = oauth.prefix = "/karox6"
    host = {"Host": "box.ts.net"}

    async def go():
        listener = await http.start("127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        async with listener, httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.post("/karox6/mcp", json=INIT, headers=host)
            assert r.status_code == 401
            meta_url = "https://box.ts.net/karox6/.well-known/oauth-protected-resource/mcp"
            assert meta_url in r.headers["www-authenticate"]
            for prefix in ("/karox6", ""):
                res = await client.get(
                    f"{prefix}/.well-known/oauth-protected-resource/mcp", headers=host
                )
                assert res.json()["resource"] == "https://box.ts.net/karox6/mcp"
            meta = (
                await client.get("/karox6/.well-known/openid-configuration", headers=host)
            ).json()
            assert meta["issuer"] == "https://box.ts.net/karox6"
            assert meta["token_endpoint"] == "https://box.ts.net/karox6/oauth/token"
            reg = await client.post("/karox6/oauth/register", json={"redirect_uris": [REDIRECT]})
            verifier, challenge = pkce()
            query = urlencode(
                {
                    "response_type": "code",
                    "client_id": reg.json()["client_id"],
                    "redirect_uri": REDIRECT,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
            page = await client.get(f"/karox6/oauth/authorize?{query}", headers=host)
            assert "action=authorize>" in page.text  # relative: stays under /karox6
            request_id = re.search(r"name=request_id value='([^']+)'", page.text).group(1)
            ok = await client.post(
                "/karox6/oauth/authorize",
                content=urlencode({"request_id": request_id, "code": oauth.pin}),
                headers=FORM,
            )
            code = parse_qs(urlsplit(ok.headers["location"]).query)["code"][0]
            tok = await client.post(
                "/karox6/oauth/token",
                content=urlencode(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": REDIRECT,
                        "code_verifier": verifier,
                    }
                ),
                headers=FORM,
            )
            bearer = {"Authorization": f"Bearer {tok.json()['access_token']}"}
            assert (await client.post("/karox6/mcp", json=INIT, headers=bearer)).status_code == 200
            assert (await client.post(f"/karox6/mcp/{TOKEN}", json=INIT)).status_code == 200

    asyncio.run(go())


def test_parallel_registrations_are_all_kept(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    oauth = OAuth(tmp_path / "oauth.json")
    body = json.dumps({"redirect_uris": [REDIRECT]}).encode()

    def register(_):
        status, _, payload = oauth.respond("POST", "/oauth/register", {"host": "h"}, body)
        assert status == 201
        return json.loads(payload)["client_id"]

    with ThreadPoolExecutor(8) as pool:
        ids = set(pool.map(register, range(40)))
    saved = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert ids <= set(saved["clients"])
    assert not list(tmp_path.glob("*.tmp"))


def test_client_metadata_is_never_fetched_from_private_addresses():
    from karox6.mcp import oauth as mod

    for url in (
        "https://127.0.0.1/client.json",
        "https://localhost/client.json",
        "https://169.254.169.254/latest",
        "https://10.0.0.5/c",
        "http://example.com/c",
    ):
        assert not mod._public_https(url), url
        assert mod._fetch_client_metadata(url) is None
