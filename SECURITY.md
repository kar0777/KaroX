# Security Policy

## Supported Versions

KaroX 5.x is in active development. Security fixes are applied to the latest
release on the `main` branch.

## Reporting a Vulnerability

Email security@karoX.dev (replace with your contact). Do **not** open a public
issue for security vulnerabilities.

- We acknowledge within 48 hours.
- We provide an estimated timeline within 5 business days.
- We credit reporters in release notes unless they prefer to remain anonymous.

## Credential Handling

KaroX stores all credentials in the operating-system keyring
(`keyring` library on Windows/macOS, Secret Service on Linux). Credentials are
**never** written to:

- stdout, stderr, or log files
- JSON configuration files
- SQLite databases (typed transcript payloads are redacted)
- Git-tracked files
- Wheel packages

The `karox bridge credential rotate-key` command and the `/connect` Detail
"Copy authorization key" action deliver secrets only via the clipboard with
a 120-second auto-clear. The `--reveal-secret` flag requires explicit `--yes`
confirmation.

## Threat Model

| Asset | Threat | Mitigation |
|-------|--------|------------|
| Bridge credential | Leak via stdout/log | Keyring-only storage; redaction at every process boundary |
| OAuth approval password | Stale after rotation | Resolved at copy-time from keyring; no plaintext in logs |
| Public tunnel URL | Unauthorized access | Bearer token required; 401 on unauthenticated requests |
| Provider API key | Exposure in transit | Keyring-backed; never passed as CLI argument in production paths |
| Workspace files | Accidental deletion | Transaction model with checkpoints; no `git reset` in undo path |

## What KaroX Does Not Do

- KaroX does not bypass CAPTCHA, 2FA, or login flows. User takeover is
  requested when these are encountered.
- KaroX does not auto-install system software without explicit consent.
- KaroX does not disable TLS verification.
- KaroX does not perform `git push`, `git commit`, or `git reset` autonomously.
- KaroX does not read passwords or paste secrets into external forms.
