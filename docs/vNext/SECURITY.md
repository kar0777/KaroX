# KaroX vNext Security Model

## Trust model

KaroX connects model-controlled and remotely initiated calls to a local machine.
Model output, hosted clients, Skills, Packs, external MCP servers, tool output,
repository content, and tunnel traffic are untrusted. The user and the local
policy store are the authority; a repository may only narrow policy.

This model reduces risk but does not make KaroX categorically safer than other
agents. Browser/desktop control and arbitrary process execution remain powerful.

## Enforcement boundary

Core Runtime is the only local-action boundary. Each request supplies session,
origin identity, capability, resource scope, correlation ID, and idempotency ID.
Enforcement computes the intersection of:

1. global user policy;
2. access profile (`read_only`, `workspace_write`, or explicitly elevated);
3. session grants;
4. origin-specific grants;
5. tool requirements;
6. a current approval/capability token when required.

Default is deny. Skills, Packs, bridge clients, and MCP servers cannot grant
themselves capabilities.

## Required controls

- Canonical repository confinement with symlink/reparse-point checks.
- No Git push, package publishing, authentication commands, or destructive
  out-of-repository operations without a visible, scoped approval.
- Mutation lease plus idempotency records for each session.
- Separate policy for `user`, `native_agent`, `hosted_client`, `skill`,
  `proxied_mcp`, and `pack` origins.
- Body/output/time/rate limits and bounded audit retention.
- Correlation IDs across provider, bridge, Core, MCP, session, and evidence.
- Emergency session revoke and bridge-key rotation.
- Explicit target-window constraints for desktop input.
- Explicit MCP tool allowlists; no implicit exposure after installation.

## Credentials

Provider and MCP secrets are stored by reference in Windows Credential Manager,
macOS Keychain, or Linux Secret Service. An encrypted fallback is allowed only
with a user-supplied or OS-protected wrapping key; there is no machine-readable
plaintext fallback.

Secret entry uses a protected prompt/stdin channel, never a command argument.
Logs, errors, HTTP headers, support bundles, session snapshots, evidence, and MCP
results pass through structural and value-based redaction. User-visible identity
is a masked fingerprint. Rotation writes the new secret before invalidating the
old reference and is audit-recorded without values.

## Network boundaries

- Local servers bind loopback by default.
- Public tunnels require an authenticated bridge profile and validated hosts.
- Bridge credentials are distinct from provider and external-MCP credentials.
- Redirects cannot forward authorization across origins.
- Custom provider URLs are validated; local/private endpoints require an
  explicit profile and cannot silently become exfiltration fallbacks.
- A provider fallback must satisfy the session privacy boundary.

## Approval queue

Approvals name the exact origin, capability, target, effect, expiry, and whether
they are one-shot or session-scoped. The queue is visible in line mode and TUI.
Approval is invalid after origin, arguments, repository fingerprint, or session
lease changes.

## Audit and evidence

Audit records are append-only diagnostics with sequence, timestamp, origin,
correlation, decision, redacted parameters, and result class. Evidence records
are immutable references to verification artifacts such as test results, Git
diff hashes, and screenshots. Neither store contains credentials. Audit is not
a substitute for authorization or transactional state.

## Threat-driven tests

- Traversal, absolute path, symlink, and repository-swap attempts.
- Empty/wrong/replayed/rotated bridge credentials.
- Cross-origin capability escalation.
- Concurrent mutation and stale lease takeover.
- Duplicate mutations after transport loss.
- Secret values in nested objects, headers, exceptions, and support bundles.
- MCP schema spoofing, namespace collision, and credential forwarding.
- Provider redirect and privacy-incompatible fallback.
- Pack/Skill setup and undeclared-tool bypass attempts.

## Known baseline gaps

The legacy server has useful confinement, redaction, audit, and no-push rules but
centralizes policy and execution in import-time global state. Origin identity,
capability tokens, shared session leases, provider-key isolation, and MCP proxy
isolation are vNext work. Phase 0 fixed missing-key startup so import succeeds
while authentication fails closed; this is covered by `test_app_entry.py`.
