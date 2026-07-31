# KaroX security model

KaroX lets AI clients request local actions in a selected Git repository. Treat
that as delegated development access with explicit limits, not as an absolute
sandbox.

## Security promise

KaroX 5 routes shipping local actions through one Core Runtime. A request is
evaluated against:

- the caller origin;
- an explicit capability;
- the selected repository identity and root;
- the active durable session;
- the current access profile;
- a mutation lease and fencing token where required;
- an idempotency key for mutating operations.

Authentication proves who is presenting a credential. It does not grant tools
or capabilities. Hosted clients see only the built-in tools and external MCP
servers explicitly selected for their session.

## What KaroX blocks

Shipping profiles are expected to block:

- path traversal outside the selected repository;
- symlink or Windows reparse-point escape;
- direct access to KaroX runtime metadata and `.git` internals where the tool
  contract does not explicitly allow it;
- known sensitive paths such as environment files, SSH keys, private-key files,
  credential stores, cookies, and token files;
- Git push and remote mutation;
- package publishing;
- destructive system-management commands;
- hidden capability escalation by Skills, Packs, providers, bridge adapters, or
  external MCP descriptors;
- hosted tools that were not explicitly selected;
- replay of a completed mutation with the same idempotency key;
- concurrent repository mutation without a valid lease;
- reporting verified success after a failed or missing check.

A protected path or command remains blocked even when a model asks confidently,
claims user approval in natural language, or places the request inside repository
content.

## What KaroX does not guarantee

KaroX is not an operating-system sandbox, container, virtual machine, or malware
analyzer.

An explicitly approved process, test, build, package script, compiler plugin, or
external MCP server executes with the operating-system rights of the KaroX
process. A repository can contain hostile build scripts. Start unknown projects
in Observe mode and inspect their build configuration before granting process or
MCP capabilities.

Repository confinement limits what KaroX tools are allowed to address. It cannot
prevent an approved third-party executable from using its own filesystem or
network APIs. Strong isolation requires an additional OS account, container,
virtual machine, or other system boundary.

KaroX also cannot guarantee the security, retention, or training policy of a
remote model provider. The user chooses which repository context is sent to a
provider and must follow that provider's terms and organizational policy.

## Access profiles

Friendly UI names map to stable policy identifiers:

- **Observe** → `read_only`: repository and Git inspection without mutation, plus
  non-mutating observation of a localhost UI — `browser.read` is snapshot, text,
  screenshot, console and failed-request inspection, with no navigation, and the
  browser session refuses any URL that is not localhost.
- **Build** → `workspace_write`: bounded file changes, approved process/check
  execution, Git status/diff evidence, selected MCP calls, and driving that local
  UI — `browser.input` is open, click, fill, select, press and close. Build does
  not grant `git.commit`.
- **Advanced** → `elevated`: explicitly adds guarded local commit plus the
  `desktop.input` and `network` capabilities, which are the ones that reach
  outside the workspace.

Browser capability used to be described here as Advanced-only, which the runtime
contradicted: the product's own tool checkbox offered browser control under
Build, and the session then refused it. The tiers above are what
`scripts/check_access_profiles.py` now pins in both directions — the presence of
`browser.read` in Observe and the absence of `browser.input` from it.

No stable profile includes Git push, package publishing, or authentication
commands. Resuming an existing session does not expand its permissions.

## Credentials and secrets

Provider, MCP, and bridge credentials use separate OS-keyring namespaces. KaroX
configuration stores opaque references, never credential values.

Credential values must not be written to:

- repository files;
- provider or model registry records;
- session state or handoff documents;
- audit logs or support bundles;
- MCP tool names, descriptions, schemas, or results;
- exception messages returned to a model;
- conformance records or public issue reports.

Redaction is defense in depth, not permission to pass secrets through arbitrary
text. Never paste an API key, bearer token, refresh token, OAuth approval
password, session credential, or private key into an AI chat.

When a credential may have been exposed, revoke or rotate it first, then collect
sanitized diagnostics.

## OAuth and hosted bridges

The ChatGPT Web and Claude Web profiles use a local approval page and remote-MCP
OAuth flow. Security-critical requirements include:

- HTTPS public origin;
- exact redirect URI matching;
- authorization code with PKCE S256;
- exact resource binding;
- short-lived access tokens;
- rotating refresh tokens;
- token-family revocation on refresh replay;
- no trust in attacker-controlled forwarding headers;
- no bearer or refresh token persisted in readable configuration;
- explicit Core and MCP tool allowlists after authentication.

A Cloudflare Quick Tunnel URL is temporary. When the public origin changes,
saved connector configuration and resource-bound grants may need to be recreated.
Do not weaken origin or resource checks to make a stale connector continue
working.

The OAuth approval password is entered only on the KaroX approval page. It is not
an app client secret and must not be pasted into ChatGPT, Claude, connector
settings, or chat messages.

## External MCP servers

An external MCP server is executable third-party code or a remote service. It is
not trusted merely because it speaks MCP.

KaroX requires explicit server selection and per-tool permission. Descriptors
exposed to a hosted client must not reveal server command lines, environment
variables, credentials, internal URLs, or other connection secrets.

Schema, identity, transport, credential, or read-only classification drift must
invalidate an earlier grant instead of silently reusing it.

## Mutation safety

A mutating Core call requires a valid session lease and stable idempotency key.
KaroX persists the outcome so a retry after a timeout can return the recorded
result instead of applying the same change twice.

Leases are serialized across processes and fenced. A stale lease holder must not
be able to write after another process has acquired a replacement lease.

Unknown outcomes must be reported as unknown, not success. The user should
inspect repository state and evidence before retrying with a new key.

## Verification and evidence

Model text is never proof that a task succeeded. A verified repository-changing
run requires durable evidence including:

- at least one real changed file;
- a successful explicitly approved check;
- Git status evidence;
- Git diff evidence.

A failed or timed-out check resets the verification chain. Evidence and handoff
records are redacted and content-digested, but they may still contain repository
metadata. Sanitize them before public sharing.

A local commit is optional and requires Advanced/elevated. Verification never
requires committing, and Build must not be upgraded merely to make an evidence
report succeed.

## Updates, migration, and rollback

Installers and updaters must stage changes, validate the staged runtime, switch
atomically, and preserve rollback. A failed update must not leave a half-written
active runtime.

Migration from KaroX 4.x defaults to dry-run, leaves the source untouched, and
never edits a user repository. Secrets are moved directly into the OS keyring or
must be re-entered securely. See
[`docs/MIGRATION_V4_TO_V5.md`](docs/MIGRATION_V4_TO_V5.md).

## Safe operating practices

- Use a disposable repository for first-run and new-client testing.
- Start unknown repositories in Observe mode.
- Review `package.json`, Makefiles, build scripts, hooks, and MCP commands before
  granting process execution.
- Keep important work committed or backed up.
- Use the smallest tool and server allowlist that completes the task.
- Inspect Git diff before requesting an Advanced local commit.
- Stop bridges and tunnels when they are not needed.
- Rotate credentials after suspected exposure.
- Do not disable push, publish, origin, resource, or path checks to work around a
  connection problem.

## Reporting a vulnerability

Do not open a public issue containing an exploit, credential, private repository
content, or unredacted support bundle. Prefer a GitHub private security advisory
for the repository when available, or contact the maintainer privately through a
published project contact.

Include:

- affected KaroX version and commit;
- operating system and Python version;
- access profile and client type;
- minimal reproduction using a disposable repository;
- expected and actual boundary behavior;
- sanitized logs or evidence;
- whether any credential or repository data may have been exposed.

Revoke exposed credentials before waiting for a response.

## Release security gates

The stable KaroX 5 release requires threat-driven coverage for traversal,
link/reparse escape, secret reflection, credential redaction, OAuth redirect and
resource binding, refresh replay, duplicate mutation, concurrent mutation,
failed verification, Git push, package publishing, update rollback, and support
bundle sanitization.

The complete release contract is
[`docs/V5_RELEASE_SCOPE.md`](docs/V5_RELEASE_SCOPE.md).
