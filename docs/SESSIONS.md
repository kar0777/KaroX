# Sessions

A session is the durable unit of work. It holds the task, the provider history,
tool evidence, changed files, checks, usage and the policy profile the run was
created with.

## Lifecycle

```text
create → active → verified | failed | stopped
                     │
                     ├── archive → unarchive → delete (with exact id)
                     └── fork (new active child)
```

- **create** binds the session to a repository fingerprint and an access
  profile.
- **run** appends every step before the next model request. A crash or a kill
  therefore loses no completed step.
- **resume** is `karox agent run --session-id ID --task "..." --continue-task`.
  The kernel loads the stored history and continues from it.
- **fork** copies lineage, plans, decisions, changed-file names and MCP
  selections, but not leases, live jobs, usage counters or provider attempt
  history. A fork starts clean because it is a new line of work.
- **archive** hides the session from the default list without deleting
  evidence. **delete** requires `--confirm` to equal the session id and only
  works on an archived session.
- **revoke** makes the session unusable without deleting its record.
- **lock** is an inspection helper: it reports whether a lease is held, by
  which owner, and whether it has expired.

## On disk

```text
<runtime>/vnext/sessions/<session-id>/session.json
<runtime>/vnext/sessions/<session-id>/...
<runtime>/vnext/audit.jsonl
```

`session.json` is written atomically with a checksum and a revision. Readers
retry a Windows sharing violation instead of treating a mid-swap read as a
corrupt session. The file contains redacted content only; credentials are never
part of it.

## Access profiles

| Profile | What a run may do |
| --- | --- |
| `read_only` | read repository, Git and diagnostics |
| `browser_control` | read-only plus browser observation and localhost input |
| `workspace_write` | the normal coding profile: read/write, checks, processes, MCP |
| `elevated` | additionally developer commands, local commits and network |

`git push`, package publishing and authentication commands are outside every
profile; they need a separate explicit approval path.

A session keeps the profile it was created with. Changing a provider's Bypass
preference does not silently re-permission a session already in flight. A new
session created while the selected provider has Bypass ON starts `elevated`.

Plan and Ideate modes remove write and commit capabilities even from an
elevated session; the mode is enforced by policy, not only by prompt text.

## Evidence

A verified run rests on recorded observations:

- changed files come from the tool result that wrote them;
- checks are recorded with their argv, exit code and bounded output;
- an answer that claims a repository fact names the inspections it used;
- a run that ends worse than a state it already proved can be restored.

The report is the authority. A UI must display `verified` and the reason, not
infer success from a finished process.