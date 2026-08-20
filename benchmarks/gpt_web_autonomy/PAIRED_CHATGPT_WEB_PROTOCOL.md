# Paired ChatGPT Web Autonomy Benchmark Protocol

## Status

`NOT RUN — USER GATE`

This protocol measures a real ChatGPT Web client. It is intentionally separate
from the deterministic server-side and simulated-client benchmark produced by
`scripts/run_gpt_web_autonomy_benchmark.py`.

The current live `clickup-opus` bridge must not be migrated, restarted, or used
as the disposable benchmark target.

## Isolation requirements

Create two clean temporary Git repositories from the same fixture snapshot:

- one for the low-level baseline profile;
- one for the optimized high-level profile.

Create two new KaroX sessions and two saved profiles:

- `chatgpt-autonomy-baseline` exposing the existing low-level tools only;
- `chatgpt-autonomy-optimized` exposing low-level tools plus `task.bootstrap`,
  `repo.inspect`, artifact-backed results, `task.execute_plan`,
  `checks.run_affected`, and checkpoint/resume.

Each profile must use:

- a separate local port;
- a separate session ID;
- a separate repository fixture;
- a separate managed-browser instance;
- a separate tunnel route only when required;
- no credentials copied from the live `clickup-opus` profile except through the
  normal KaroX saved-profile mechanism and only after explicit user approval.

Do not expose personal Chrome, keyring secrets, clipboard content, external
messaging, payments, deployment, publish, Git push, or destructive Git.

## Client setup

Open two fresh ChatGPT Web chats with the same model, effort, language, and
context. Connect one chat to each isolated profile. Do not preload either chat
with repository details beyond the task text.

Before task 1:

1. Confirm both profiles report the expected repository and session.
2. Export `karox.bridge.diagnostics` for each profile.
3. Confirm the baseline profile does not expose high-level tools.
4. Confirm the optimized profile exposes the versioned high-level tools.
5. Confirm telemetry contains no arguments or file contents.

## Task set

Run the same ten tasks in the same order in both chats:

1. Find a function implementation and its related tests.
2. Build the flow from a TUI command to a backend service.
3. Diagnose a focused test failure.
4. Add a small validation rule.
5. Change a function and update its related tests.
6. Perform a multi-file refactor.
7. Select and run affected checks.
8. Start an owned dev server and verify readiness.
9. Inspect the fixture UI through the managed browser.
10. Resume a checkpointed task in a fresh chat/session after restart.

Use an identical task prompt for each pair. Do not tell the baseline chat which
specific low-level calls to make and do not tell the optimized chat which
high-level tool to use.

## Evidence collected per task

Collect from KaroX telemetry and task artifacts:

- task success;
- total tool calls;
- repeated calls/reads;
- input argument bytes;
- inline result bytes;
- artifact bytes;
- wall-clock duration;
- validation and permission failures;
- retries and cache hits/misses;
- user interventions;
- recovery success;
- safety violations;
- incorrect pre-existing-failure classification;
- duplicate side effects during replay.

The evaluator must inspect the resulting repository diff and verification
evidence. A model statement alone is not success evidence.

## Pairing and fairness rules

- Start each task from equivalent fixture revisions.
- Reset only the disposable fixture repositories, never the live KaroX tree.
- Use the same verification allowlist and execution budgets.
- Preserve failed runs; do not discard outliers.
- Record reconnects, malformed calls, and retries instead of silently rerunning.
- Do not count KaroX internal sub-operations as ChatGPT tool calls; report them
  separately as server-side operations.
- Report both inline and total result bytes so artifact offloading is not hidden.

## Acceptance targets

The optimized profile must achieve:

- task success not below baseline;
- at least 35% lower median ChatGPT tool calls;
- at least 50% lower median inline context bytes;
- 100% resume success on recovery tasks;
- zero unauthorized writes;
- zero secret leaks;
- zero duplicate side effects during idempotent replay.

## Stop conditions

Stop immediately before any request involving:

- OAuth consent or account changes;
- password, CAPTCHA, or 2FA;
- payment or paid API usage;
- external messages or form submission;
- permission or repository-scope expansion;
- live-profile migration;
- Git push, publish, deployment, release, or destructive operations.

## Result status

Until this protocol is run with two fresh ChatGPT Web chats, documentation must
show:

`paired_chatgpt_web.status = not_run_user_gate`

The deterministic benchmark must never be presented as real ChatGPT Web model
performance.
