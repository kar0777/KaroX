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

## Access Profiles

KaroX exposes three stable access profiles. The friendly names used in the UI map
to durable policy identifiers:

- **Observe** -> `read_only`: repository and Git inspection plus `browser.read`
  for non-mutating observation of the localhost UI. It does not grant
  `browser.input` or repository mutation.
- **Build** -> `workspace_write`: repository mutation, approved process/check
  execution, Git status/diff, selected MCP calls, `browser.read`, and
  `browser.input` for driving the localhost UI. Build does **not** grant
  `git.commit`.
- **Advanced** -> `elevated`: Build capabilities plus guarded local commit,
  `desktop.input`, and `network` for explicitly elevated work.

Even Advanced does not grant standing `git push`, package publishing, or
authentication authority. Those effects remain outside every stable access
profile. A durable Advanced ChatGPT Web bridge may expose the dedicated
`karox.git.push` operation, but each invocation crosses a separate one-shot user
approval bound to the exact action; ordinary developer commands remain unable to
push. Modern MCP clients use the native elicitation round. If a client does not
advertise elicitation, KaroX falls back to a short-lived browser approval page
bound to that same exact action. The user enters the OAuth approval password
there; the password is never returned to the MCP client or model, and the
approval is consumed by the first matching retry.

## What KaroX Does Not Do

- KaroX does not bypass CAPTCHA, 2FA, or login flows. User takeover is
  requested when these are encountered.
- KaroX does not auto-install system software without explicit consent.
- KaroX does not disable TLS verification.
- KaroX does not perform `git push` or `git reset` autonomously. A guarded local
  commit is available in Advanced/`elevated`; remote push requires the dedicated
  exact-action one-shot user approval and never authorizes force-push.
- KaroX does not read passwords or paste secrets into external forms.