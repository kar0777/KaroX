# ChatGPT Web live conformance

integration: chatgpt-web
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

Pending a run against a real ChatGPT workspace. Local OAuth and MCP contract
coverage does not count as a live product pass.

## Limitations

Pending.
