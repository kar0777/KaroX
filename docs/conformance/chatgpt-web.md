# ChatGPT Web live conformance

integration: chatgpt-web
status: pending
release_blocker: true
verified_at_utc: pending
karox_version: 5.0.0rc1
karox_commit: dbd3539fb552434c1370b8c0e67ea6475262670e (release work on top is uncommitted)
platform: windows-11-x64 (Tailscale Funnel HTTPS ingress)
external_version: ChatGPT web connector platform (SaaS, uncontrolled)
account_tier: pending
evidence: docs/HANDOFF_2026-09-15_OAUTH_BRIDGE_RECOVERY.md (partial run, not a full pass)

## Required scenario

- create or select a repository-bound Build session;
- publish the OAuth remote-MCP bridge through an HTTPS URL;
- complete discovery, dynamic client registration, authorization code with PKCE,
  and KaroX approval;
- initialize MCP and list only explicitly selected tools;
- read one repository file;
- perform one bounded file mutation;
- retry the same mutation without duplicating its effect;
- run an approved verification command;
- read Git status and diff;
- refresh an expired access token and verify rotation;
- restart KaroX and document whether the connector URL or registration must be
  updated;
- produce a sanitized evidence report.

## Result

**Partial evidence, not a PASS** (2026-09-15 recovery session, Windows host with
the Tailscale Funnel bridge `chatgpt-dev` at
`https://monsterpc.taila81286.ts.net`):

Observed live, verified by the user's working connector (rename pinned below):

- a ChatGPT connector was created against the stable funnel URL and completed
  OAuth: discovery metadata, dynamic client registration, authorization code
  with PKCE (S256), the KaroX approval page (password entry), and token
  exchange all succeeded end-to-end (`karo1` connected; the OAuth bridge state
  file records two registered ChatGPT clients with redeemed codes and
  persisted refresh grants for that resource);
- the hosted ChatGPT client refreshed the `/mcp` tools surface successfully
  after the fix wave (green "Действия обновлены" state on the connector);
- OAuth discovery now answers every probe spelling ChatGPT is known to use,
  including `/.well-known/oauth-authorization-server/mcp` and
  `/mcp/.well-known/*` forms, and DCR accepts an authorization_code-only grant
  set (both covered by deterministic wire tests);
- durable identity survived multiple child/owner recycles during the session:
  same public URL, same session id, same OAuth state across restarts.

Not yet observed live, therefore still required for PASS:

- executing a repository read, one bounded mutation and its idempotent retry
  **through the real ChatGPT session** (local tool coverage exists; the hosted
  client only listed tools this session);
- run of an approved verification command and Git status/diff evidence round
  trip through ChatGPT;
- explicit access-token expiry + refresh rotation observation;
- a full KaroX restart with a reconnection check from the ChatGPT side;
- sanitized end-to-end evidence report with connector screenshots.

Known external limitation: individual Tailscale Funnel POPs (observed on
`ro1`/`uk1`) intermittently time out end to end while most POPs answer, which
can surface on the ChatGPT side as a transient "does not implement OAuth" /
connection error even while KaroX is healthy. KaroX-side failure modes for this
session were fixed and are covered by tests; the POP reliability itself is
outside KaroX control.

## Result (previous)

Pending a run against a real ChatGPT workspace. Local OAuth and MCP contract
coverage does not count as a live product pass.

## Limitations

The partial live verification above was performed against a working developer
connector configured by the user; it does not exercise every listed scenario
and it does not switch the block on its own. Keep the record pending until a
deliberate full scenario run is captured.
