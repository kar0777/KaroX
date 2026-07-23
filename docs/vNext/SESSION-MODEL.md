# KaroX Unified Session Model

## Ownership

A session belongs to KaroX. Providers and hosted clients attach to it; they do
not own its durable state. Full chat history is optional provider context, not
the recovery source of truth.

## Durable record

Each versioned session contains:

- session ID, timestamps, repository path/fingerprint, and branch;
- access profile, grants, connected clients, and active mutation lease;
- original task, structured plan, current phase, summary, and decisions;
- changed files, Git status/diff digest, checkpoints, and pending mutations;
- checks, failures, evidence, jobs, dev servers, and reconciliation state;
- selected Skills, Packs, MCP servers, and exact versions;
- provider/model history, usage, estimated cost, and budgets;
- audit cursor, unfinished actions, approvals, and revocation state.

Repository state is referenced by fingerprint and verified on resume. The record
does not duplicate arbitrary repository content or credentials.

## Storage

Snapshots are written to a temporary file, flushed, and atomically replaced.
Monotonic revision and checksum detect stale/corrupt writes. Evidence and large
outputs are content-addressed separately with limits. Corruption is quarantined
and reported; it never produces a blank “successful” session.

## Locking

One exclusive mutation lease is allowed per session. It records owner identity,
process instance, revision, acquisition/heartbeat/expiry time, and a random
fencing token. Read-only clients may attach concurrently. A stale lease can be
taken over only after process/liveness reconciliation; every mutation validates
the current fencing token before commit.

Explicit multi-agent mode assigns non-overlapping scopes or serializes writes.
It does not disable the lease.

## Structured handoff

The handoff document includes:

- goal and constraints;
- completed work and decisions;
- changed files and current Git state;
- commands and verification outcomes;
- errors and unresolved risks;
- active/unknown processes;
- selected capabilities, Skills, Packs, and MCP tools;
- provider/model/usage history without secrets;
- evidence references and next steps.

The receiver must reconcile repository fingerprint, branch, jobs, and lease
before mutation. A provider switch adds history and constructs new model context
from this structure plus targeted retrieval; it does not blindly copy all chat.

## Mutation protocol

1. Acquire/validate lease and policy.
2. Reserve an idempotency key at the current session revision.
3. Persist intent and bounded input digest.
4. Execute through Core.
5. Persist result, repository observation, and evidence atomically.
6. Return the stored result.

A retry with the same key and input returns the stored result. The same key with
different input is rejected. Unknown outcomes enter reconciliation rather than
automatic replay.

## Compaction invariants

Compaction cannot discard the goal, constraints, user decisions, plan, changed
files, current failures, verification state, pending approvals/mutations, usage
budgets, or evidence references. Summaries record provenance and source revision.

## Privacy and retention

Session export excludes credentials and redacts secrets. Retention is explicit,
with separate controls for snapshots, audit, evidence, and provider transcripts.
Deletion refuses while a live lease/job exists unless the user confirms forced
revocation.
