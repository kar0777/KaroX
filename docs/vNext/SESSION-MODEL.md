# KaroX Unified Session Model

This file describes `src/karox/sessions.py`, `handoff.py`, and the compaction in
`agent.py`. Fields that exist in the record but that nothing writes are called
out, and items with no implementation are marked **Planned**.

## Ownership

A session belongs to KaroX. Providers and hosted clients attach to it; they do
not own its durable state. Full chat history is optional provider context, not
the recovery source of truth.

## Durable record

`SessionRecord` is versioned by `schema_version` and checksummed. Loading rejects
an unknown schema, an unknown field, a missing checksum, and a checksum mismatch.

Fields with a real writer today:

- `session_id`, `created_at`, `updated_at`, `repository`, `repo_fingerprint`,
  `branch`, `access_profile`, `task`, `revision`, `status`, `revoked`;
- `phase` and `summary` — written by the native agent loop;
- `changed_files`, `git_state`, `checks`, `failures`, `evidence` — written by Core
  and by the agent as work happens;
- `provider_history` and `usage` — written per model call, redacted;
- `skills` and `mcp_servers` — written by `karox skill`/`karox mcp` selection;
- `idempotency` — the reserved mutation keys.

Fields that are **declared and read but never written** by anything in
`src/karox`: `plan`, `decisions`, `checkpoints`, `jobs`, `connected_clients`,
`packs`, and `unfinished_actions`. `handoff.py` reads all of them, so a handoff
document faithfully reports empty lists. Treat them as reserved shape, not as
recorded state.

**Not present at all:** there is no grants field, no dev-server registry, no
reconciliation-state field, no audit cursor, no approvals list, and no budget
field on the record. Budgets live in `RoutingPolicy`, per call, not in the
session. The active mutation lease is a separate `lease.json` beside the state
file rather than a field inside it.

Repository state is referenced by fingerprint and verified on resume. The record
does not duplicate arbitrary repository content or credentials.

## Storage

Snapshots are written to a uniquely named temporary file, flushed, `fsync`ed, and
atomically replaced. A monotonic `revision` and a checksum over the payload
detect stale and corrupt writes; a corrupt snapshot raises `SessionError` and
never produces a blank "successful" session. `list()` skips records it cannot
verify rather than reporting them as healthy.

**Planned:** content-addressed storage for evidence and large outputs. There is
no separate artefact store and no per-artefact size budget. What exists is a
`sha256` digest recorded *inside* the evidence entry (`artifact_sha256`) as a
fingerprint of what was written or checked, plus per-result truncation limits.

**Planned:** quarantine. A corrupt snapshot is reported and refused; it is not
moved aside into a quarantine area. The one thing that is set aside is a stale
`lease.json`, renamed to `lease.stale.<hex>.json` when a new owner takes over.

## Locking

One exclusive mutation lease is allowed per session. `MutationLease` records the
owner, the session revision, the process instance (`pid` and `hostname`),
acquisition and expiry time, and a random fencing token. `heartbeat` extends the
expiry; every mutation revalidates the current
fencing token before commit, so a superseded owner is fenced out rather than
merely warned. Read-only clients may attach concurrently. A stale lease can be
taken over only after its expiry has passed.

**Planned:** an explicit multi-agent mode. There is no scope assignment and no
write serializer beyond the single lease; a second agent simply cannot acquire
the lease while the first holds it.

## Structured handoff

`handoff.build_handoff` produces a bounded, redacted document containing the
task, repository/fingerprint/branch, access profile, selected Skills and MCP
servers, summary, decisions and checkpoints, changed files, executed check
commands and their outcomes, errors, remaining plan steps, Git state, active
jobs, model history, usage, unfinished actions, and evidence references
(kind, summary, digest, id). Secrets are redacted and lists are length-capped.

Because `decisions`, `checkpoints`, `plan`, `jobs` and `unfinished_actions` have
no writer, those sections are structurally present and empty in practice. The
document is not a "goal and constraints" record: there is no constraints field.

The receiver reconciles the repository fingerprint and branch and must hold the
lease before mutating. A provider switch adds history and constructs new model
context from this structure; it does not blindly copy all chat.

## Mutation protocol

1. Acquire/validate lease and policy.
2. Reserve an idempotency key at the current session revision.
3. Persist intent and bounded input digest.
4. Execute through Core.
5. Persist result, repository observation, and evidence atomically.
6. Return the stored result.

A retry with the same key and input returns the stored result. The same key with
different input is rejected. An unknown outcome is reported as unknown and is not
replayed automatically.

## Compaction invariants

Compaction applies to the **model request history**, not to the session record —
the record is never compacted, so nothing in it is at risk of being dropped.

What `Agent._compact` guarantees: the system prompt and the original task are
never compaction candidates; a tool call is never separated from its result;
recent turns survive even when they exceed the ceiling; older turns are replaced
by a `context_summary` entry recording what it stands for; oversized tool results
are clipped in the outgoing request only, never in stored history; and an unknown
context window still gets a safety ceiling.

## Privacy and retention

Redaction runs over everything persisted from a provider or a tool result, and
the handoff document is redacted again on the way out. Credentials are never
stored in the record — only references into the credential store.

**Planned:** session export and retention controls. There is no `session export`
command and no retention configuration, so there are no separate controls for
snapshots, audit, evidence, or provider transcripts. `session revoke` marks a
session revoked, bumps its revision, and removes the lease; there is no delete
command, so nothing has to refuse a delete while a lease is live.
