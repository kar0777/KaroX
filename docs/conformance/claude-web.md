# Claude Web live conformance

integration: claude-web
status: pending
release_blocker: true
verified_at_utc: pending
karox_version: 5.0.0.dev0
karox_commit: pending
platform: pending
external_version: pending
account_tier: pending
evidence: pending

## Required scenario

- create or select a repository-bound Build session;
- add the KaroX Streamable HTTP MCP connector through HTTPS;
- complete OAuth discovery, registration, PKCE authorization, and approval;
- initialize MCP and expose only the selected tools;
- read and change a repository file;
- prove mutation idempotency;
- run an approved verification command;
- read Git status and diff;
- rotate or refresh credentials as supported by the client;
- restart KaroX and record connector persistence behavior;
- produce a sanitized evidence report.

## Result

Pending a run against a real Claude paid account. Local OAuth and MCP contract
coverage does not count as a live product pass.

## Limitations

Pending.
