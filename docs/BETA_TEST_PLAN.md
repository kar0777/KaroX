# KaroX 5 external beta test plan

Status: planned  
Target runtime: `5.0.0rc1` and later release candidates

The beta tests the product, not the maintainer's ability to guide a user through
it. Testers receive a build, the public quick start, and an issue/report form.
They should not receive a private walkthrough before the first attempt.

## Goals

The beta must answer:

- Can a new user install KaroX without maintainer intervention?
- Can the user understand Observe, Build, and Advanced?
- Can the user connect one supported client?
- Can the client complete the primary write-check-diff-evidence scenario?
- Does restart/resume avoid duplicate mutation?
- Does the user understand what KaroX protects and what it does not sandbox?
- Would the user use KaroX for a second real task?

## Tester profile

Recruit at least ten candidates to obtain five complete attempts. Prefer a mix
of:

- ChatGPT users with access to developer/custom MCP connections;
- Claude users with custom connector access;
- developers using OpenAI, Anthropic, Gemini, or compatible APIs;
- maintainers of private or security-sensitive repositories;
- developers who regularly switch model providers;
- Windows, macOS, and Linux users;
- at least two people who have never seen KaroX internals.

Do not select only friends who will tolerate manual fixes or undocumented steps.

## Test repository

Use a disposable repository containing:

- a small application or library;
- one deterministic test command;
- one intentionally failing test or small defect;
- no real credential;
- no private production data;
- enough Git history to make status and diff meaningful.

The same baseline repository should be available for every tester so outcomes
can be compared. Testers may repeat the flow on their own repository only after
the disposable run.

## First-attempt task

The tester receives only this task:

1. Install the supplied KaroX build.
2. Run `karox` in the disposable repository.
3. Start in Observe and inspect the project.
4. Switch to Build through the documented UI.
5. Connect ChatGPT Web, Claude Web, or one API provider.
6. Ask the agent to fix the supplied defect.
7. Approve the documented test command.
8. Inspect Git status, Git diff, and the evidence report.
9. Restart KaroX and resume the session.
10. Confirm the mutation was not applied twice.

The maintainer may answer only after the tester records the exact step where the
first unaided attempt stopped.

## Data to record

For every attempt record:

- tester ID or pseudonym;
- date and timezone;
- operating system and version;
- Python version;
- KaroX version and commit;
- installation method;
- selected client/provider and visible version/tier;
- time to first successful `karox` launch;
- time to connected client;
- time to first verified task;
- number of documentation searches;
- number of maintainer interventions;
- exact blocker and error text;
- whether permission profiles were understood;
- whether the final evidence was trusted;
- whether restart/resume worked;
- whether the tester used KaroX for a second task;
- one thing that felt unnecessary;
- one missing capability that actually blocked work.

Never record credentials, private source, or an unsanitized network capture.

## Outcome classification

- **Complete unaided** — primary scenario completed without maintainer help.
- **Complete assisted** — completed after at least one maintainer intervention.
- **Blocked by product** — install, connection, mutation, verification, or resume
  could not be completed because of KaroX.
- **Blocked by account/environment** — required third-party entitlement or local
  system component was unavailable and KaroX diagnosed it correctly.
- **Abandoned** — tester stopped for usability, trust, or time reasons.

Account/environment blockers count against onboarding when the diagnostic does
not clearly explain the missing requirement and next action.

## Stable-release beta gates

Before stable `5.0.0`:

- at least five external testers attempt installation without a walkthrough;
- at least three complete the primary scenario;
- at least two complete it without maintainer intervention;
- at least two voluntarily perform a second task;
- every P0 data-loss or credential-exposure issue is closed and regression-tested;
- no more than one of the last five attempts is blocked by installation;
- no more than one of the last five attempts is blocked by unclear permissions;
- every supported client has at least one passed live conformance record.

## Triage priority

Fix during beta in this order:

1. credential exposure or repository data loss;
2. update/rollback corruption;
3. installation or launcher failure;
4. OAuth/connector failure in the primary flow;
5. duplicate mutation or broken resume;
6. false verified success;
7. unclear permission or security messaging;
8. non-actionable diagnostics;
9. performance and visual polish;
10. preview-feature defects unrelated to the primary flow.

Do not expand scope to satisfy a single feature request unless it blocks the
primary scenario for multiple testers.

## Beta summary

Maintain a sanitized aggregate summary at
`docs/conformance/beta-summary.md`. The summary may contain counts and themes,
not personal data or private repository content.
