# Handoff: ChatGPT OAuth bridge recovery — 2026-09-15

Author: ZCode session (oauth-bridge debugging task). Audience: the next coding
agent working on KaroX 5. Read this before touching the web-bridge/OAuth stack.

## Outcome (verified by the user)

- ChatGPT connector `karo1` (app id `asdk_app_6a8bf6e8503881919dc2c878df864047`,
  URL `https://monsterpc.taila81286.ts.net/mcp`) reconnected successfully and
  ChatGPT confirmed **"Действия обновлены"** (actions refreshed) at ~17:41.
- The earlier hard failure was ChatGPT's connector-creation toast
  **"MCP server https://monsterpc.taila81286.ts.net/mcp does not implement
  OAuth"**, followed (after an interim fix) by **"Ошибка обновления действий"**.

## Symptom timeline

- ~16:33 user restarts the bridge (`bridge connect --saved chatgpt-dev`).
- ~16:39–16:43 ChatGPT connector creation fails with the "does not implement
  OAuth" toast. Browser-side authorize/approve DID run twice: two PKCE-bound
  authorization codes were minted at 16:42:54 / 16:43:06 and **never
  redeemed** (`codes` stayed in the persisted OAuth state until expiry).
- ~17:04 a further authorize went pending but was never approved by the user
  (approval page left unanswered) — later surfaced as "Ошибка обновления
  действий".
- ~17:10 the codes from an earlier round were redeemed (state `codes: 0`,
  one new refresh token), i.e. the OAuth chain then worked end to end.
- After the runtime patch + controlled restart, actions refresh succeeded.

## Root-cause picture (evidence, not hunches)

1. **Not a code regression.** `src/karox/oauth_bridge.py` was byte-identical
   to HEAD (and to the installed 5.0.0rc1 copy) before this session; Notion /
   Tasklet / ClickUp / indent clients registered successfully through the same
   code on Sep 6–12.
2. **Discovery-alias gap.** ChatGPT's validation probes more well-known
   spellings than the docs admit; the live server returned 404 for
   `/.well-known/oauth-authorization-server/mcp` and
   `/mcp/.well-known/...` (see the volunteers' probe list in OpenAI community
   topic 1365324; OpenAI's own auth docs: developers.openai.com/plugins/build/auth).
   A single 404 there is read by the platform as "does not implement OAuth".
3. **DCR grant strictness.** `register()` required
   `grant_types == ["authorization_code","refresh_token"]` exactly; ChatGPT
   registers with `["authorization_code"]` only (recorded in
   `<state>.register-probe.json`). RFC 7591 says the server decides what it
   grants, so a subset including the code grant is now accepted.
4. **Flaky funnel reachability (OS-side, unresolved by us).** From 56
   check-host.net POPs, 54 fetched the well-known URL with HTTP 200, but
   `ro1`/`uk1` repeatedly timed out end-to-end while TCP connect succeeded —
   i.e. some Tailscale funnel ingress POPs could not reach the device. This
   matches OpenAI community reports of intermittent "does not implement
   OAuth" where "some IPs get through, some don't" (topic 1386102). Redeem
   failures (codes issued but not posted to `/oauth/token`) are the exact
   signature of the user's 16:39–16:43 window.
5. The OAuth state file `d3b245ef...json` (hashed-only payloads) plus the new
   request trace supplied every hard fact above.

## Changes made (working tree, uncommitted)

- `src/karox/oauth_bridge.py`
  - AS/protected-resource discovery routes now also serve the RFC 8414
    path-inserted form (`/.well-known/oauth-authorization-server{path}`) and
    the MCP-wrapped form (`{path}/.well-known/...`) for both
    `oauth-authorization-server` and `openid-configuration` (canonical routes
    unchanged). Protected resource gained the wrapped form too.
  - `register()` accepts any subset of
    `{authorization_code, refresh_token}` that contains `authorization_code`
    (duplicates and unknown grants still rejected); the bridge still grants
    only the code(+refresh) flow it implements.
  - Request trace: every request reaching the OAuth edge appends
    `{"method","path"}` (no query/headers/bodies/tokens) to
    `<state>.request-probe.jsonl` with a 256 KiB / 512-line cap. Replaces the
    Notion-only trace. Watch for the `is_file()` guard before `stat()` —
    v1 of this patch never created the file because `stat()` on a missing
    file raised and was swallowed.
- `tests/test_oauth_bridge.py`
  - `test_discovery_aliases_cover_path_inserted_and_mcp_wrapped_forms`
  - `test_registration_accepts_an_authorization_code_only_client`
  - `test_registration_still_rejects_codeless_or_unknown_grants`
- `src/karox/hosted_bridge.py` — added the missing `Mapping` typing import
  (pre-existing WIP regression caught by mypy at line 710; unrelated to this
  fix but was breaking the gate).
- `docs/RELEASE_CHECKLIST.md`, `README.md`, `docs/vNext/README.md` — test
  counts refreshed via `python scripts/check_test_count.py --write`
  (3343→3347 suite, 3348→3352 root collection).

The installed package under
`C:\Users\ekono\AppData\Local\Programs\Python\Python313\Lib\site-packages\karox`
is an editable/hardlinked install of this repo (same inode), so the running
bridge picked the patched module up after a restart. **Deleted a stray
`oauth_bridge.py.5.0.0rc1.bak`** attempt — do not leave .bak files there.

## Operational actions taken

- Verified live endpoints (all through the funnel):
  `/.well-known/oauth-authorization-server(/mcp)`, `/mcp/.well-known/...`
  aliases → 200; DCR with grant subset → 201; `/mcp` GET → 401 with
  `WWW-Authenticate resource_metadata`.
- Ran `python -m karox.cli bridge restart --saved chatgpt-dev` twice; the
  second restart's CLI hung and a killed background owner left the funnel
  serve-config missing ("No serve config") and tailscale in transient
  `NoState`. **Recovered by re-running
  `python -m karox.cli bridge connect --saved chatgpt-dev` (detached)**; it
  restores the `tailscale funnel` targets (443→127.0.0.1:8765 and
  8443→127.0.0.1:8767). Lesson: `bridge restart`'s CLI can hang after a
  successful stop; check `karox bridge status --saved chatgpt-dev` before
  assuming anything, and never `TaskStop` the background `bridge connect`
  owner.
- Current live child: PID 13912 on 127.0.0.1:8765 (was 10636 → 11352 → 16868
  → 13912); funnel healthy; tailscale account `fkonohovich@`, no AAAA on the
  LAN.

## Verified good

- Targeted pytest: `test_oauth_bridge.py`, `test_hyperagent_bridge.py`,
  `test_oauth_cimd.py`, `test_oauth_clickup_redirect_chain.py`,
  `test_web_bridge_profiles.py`, `test_hosted_bridge_smart_stop.py` — pass
  (1 pre-existing skip).
- `ruff check src tests scripts` clean; `mypy` clean for
  `oauth_bridge.py` + `hosted_bridge.py`; `git diff --check` clean on touched
  files; `check_test_count.py` consistent after `--write`.

## Not done / left for the next agent

- Full pytest suite, repo-wide mypy and wheel build were **not** run (large
  WIP tree belongs to the release owner). Do them before any release claim.
- `python scripts/check_versions.py` reports a pre-existing red unrelated to
  this task: "access profiles: README_RU.md does not state the stable
  push/publish boundary" — that file is part of the user's WIP.
- The funnel POP flakiness (ro1/uk1 timeouts) is infra-side; if users report
  intermittent connector failures again, re-run the check-host net probe
  pattern below before blaming the bridge.
- `docs/conformance/chatgpt-web.md` is still `status: pending` — today's live
  success (connect + approve + token + tools refresh) is a good data point
  but not a recorded conformance pass.
- No commits were made (policy). My changes sit in the working tree on
  `feat/karox-v5-competitive-upgrade`.

## How to observe next time

- OAuth state (no bearer secrets — digests only):
  `%LOCALAPPDATA%\KaroX\vnext\oauth-bridge\d3b245ef2b7f54cf7b4651a78ff15173.json`
  (clients/codes/pending; `created_at` is a unix ts).
- Request trace (method+path only):
  same dir, `d3b245ef...request-probe.jsonl`.
- DCR payload probe (overwritten per registration, schema-level only):
  `d3b245ef...register-probe.json`.
- Status: `python -m karox.cli bridge status --saved chatgpt-dev`;
  funnel: `tailscale funnel status`.
- External reachability (no secrets exposed; hostname is already public):
  `https://check-host.net/check-http?host=https%3A%2F%2Fmonsterpc.taila81286.ts.net%2F.well-known%2Foauth-authorization-server`
  with `Accept: application/json`, then poll `check-result/{request_id}`.

## ChatGPT interop references

- OpenAI auth docs: https://developers.openai.com/plugins/build/auth —
  resource metadata via `WWW-Authenticate` or `/.well-known/oauth-protected-resource`,
  AS metadata at `/.well-known/oauth-authorization-server` / openid alias,
  RFC 9207 `iss` for the stable redirect
  `https://chatgpt.com/connector_platform_oauth_redirect`, otherwise
  callback-specific `https://chatgpt.com/connector/oauth/{callback_id}`,
  PKCE S256 mandatory, CIMD preferred when
  `client_id_metadata_document_supported` is true.
- Community evidence: topics 1365324 (probe list: POST /mcp, both well-known
  families + `/mcp/.well-known/*` + base `/`), 1386102 (intermittent =
  egress/IP-blocked, no request reaches server during failures), 1394731
  (validation UA `curl/8.5.0`; WAFs that block it produce the same toast).
