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
- Separate explicit allowlists for built-in Core tools and external MCP tools;
  no implicit exposure after installation.

## Credentials

Provider and MCP secrets are stored by reference in Windows Credential Manager,
macOS Keychain, or Linux Secret Service. An encrypted fallback is allowed only
with a user-supplied or OS-protected wrapping key; there is no machine-readable
plaintext fallback.

Secret entry uses a protected prompt/stdin channel, never a command argument.
Logs, errors, HTTP headers, support bundles, audit records, and MCP result
metadata pass through structural and value-based redaction. User-visible identity
is a masked fingerprint. Rotation overwrites the stored value atomically from
the runtime's perspective; the wire server resolves it on every request so the
old bearer stops authorizing immediately.

Repository **content** is deliberately exempt from pattern-based rewriting.
`repo.read_file`, `repo.read_lines`, and `repo.search` return the bytes that are
on disk, and a read whose body does not hold the whole file says so with
`truncated`, `content_sha256`, and a `detail` field.

This is a correctness requirement, not an oversight. Pattern rewriting inside
file content changed a token-shaped literal in real source into `[REDACTED]`, so
an exact-match edit anchor taken from a read could not match the file it came
from, and echoing that content back through a write destroyed the original line.
It also clamped the body at one million characters while reporting the whole
file's byte count and digest, which lost data with no error.

The pattern list covers four shapes, so it never was a general content-privacy
control: a password, a private key body, or a cloud access key passed through it
untouched. Reads therefore report `secret_like` when the content matches one of
those shapes, so a caller and the user are told rather than silently handed
altered text. Credential *values* KaroX itself holds are still removed on every
path, and keys whose name looks like a credential are still replaced.

If a repository must not be shown to a model at all, that is a decision about
which repository to point KaroX at, not something a four-pattern regular
expression can enforce.

## Network boundaries

- Local servers bind loopback by default.
- Public tunnels require an authenticated bridge profile and validated hosts.
- Bridge credentials are distinct from provider and external-MCP credentials.
- MCP and OpenAPI bridge requests use the same dynamically resolved credential;
  OpenAPI accepts exactly one of Bearer or `X-API-Key` authentication.
- ChatGPT/Claude web profiles expose OAuth metadata only for one configured
  HTTPS origin and MCP resource. They require DCR, Authorization Code, PKCE
  S256, exact redirect/resource binding, short-lived access tokens, and rotating
  refresh tokens. Refresh replay revokes the token family.
- The bridge credential is the OAuth approval-page password for web profiles;
  it is never returned to the MCP client. Persisted OAuth state holds SHA-256
  digests, never a bearer token, under mode 0600 and bound to the issuing
  resource URL; a grant is dropped rather than honoured when the file does not
  say exactly what it claims, and a persisted family revocation is not undone by
  a restart.
- Redirects cannot forward authorization across origins.
- Custom provider URLs are validated; local/private endpoints require an
  explicit profile and cannot silently become exfiltration fallbacks.
- A provider fallback must satisfy the session privacy boundary.

## Approval state

Core supports origin-bound, expiring one-shot capability tokens for the
always-explicit operations. A general visible approval queue in line mode/TUI
is not implemented; the documentation does not treat it as a release feature.

## Audit and evidence

Audit records are bounded, rotated JSONL diagnostics with timestamp, origin,
correlation, decision, redacted parameters, and result class. They are not a
tamper-evident ledger. Evidence records are checksum-protected session data with
artifact hashes, not an external immutable store. Neither contains credentials;
neither substitutes for authorization or transactional state.

## Threat-driven tests

- Traversal, absolute path, symlink, and repository-swap attempts.
- Empty/wrong/replayed/rotated bridge credentials.
- OAuth metadata, hostile redirect registration, wrong PKCE/resource/client
  bindings, one-use authorization codes, refresh rotation, and replay
  revocation.
- OpenAPI schema exposure, session binding, missing mutation idempotency, and
  built-in Core read/write calls over a real HTTP server.
- Cross-origin capability escalation.
- Concurrent mutation and stale lease takeover.
- Duplicate mutations after transport loss.
- Secret values in nested objects, headers, exceptions, and support bundles.
- MCP schema spoofing, namespace collision, and credential forwarding.
- Provider redirect and privacy-incompatible fallback.
- Pack/Skill setup and undeclared-tool bypass attempts.

## Known baseline gaps

The legacy server still centralizes policy and execution in import-time global
state and does not share vNext origin identities or leases. vNext implements
those controls in `src/karox`; the legacy path remains contained for coexistence
and is not represented as having the same isolation. Missing-key startup fails
closed and remains covered by `test_app_entry.py`.
