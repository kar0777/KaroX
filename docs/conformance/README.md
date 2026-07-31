# Live conformance records

This directory records evidence from real third-party products and provider
accounts. Deterministic local servers prove KaroX protocol contracts; they do
not prove that a named external product accepts the same flow today.

A record has one of four statuses:

- `pending` — no complete live run is recorded;
- `passed` — the required scenario completed and evidence is attached;
- `failed` — a live attempt was made and did not complete;
- `not-applicable` — allowed only with a written release-scope rationale.

## Required release records

KaroX 5.0 requires passed records for:

- `chatgpt-web.md`;
- `claude-web.md`;
- `openai-responses.md`;
- `anthropic-messages.md`;
- `gemini.md`;
- `openai-compatible.md`.

## Evidence rules

A `passed` record must include:

- UTC verification date;
- exact KaroX version and commit;
- operating system and Python version;
- external client, account tier, API, or model version where visible;
- the exact scenario exercised;
- limitations and anything not tested;
- a sanitized evidence reference that contains no access token, refresh token,
  API key, repository source, or private user data.

Do not paste credentials or unredacted HTTP captures into this directory.
Screenshots and logs should be sanitized before they are referenced.

The static gate `python scripts/check_v5_release.py` validates record structure.
It intentionally refuses a final `5.0.0` runtime while any required record is
still pending or failed. Development versions may keep pending records so work
can continue honestly.
