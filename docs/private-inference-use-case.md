# Private inference use case

> **Status:** design and evaluation track. KaroX does not currently claim that every connected model provider offers confidential computing. This document defines the use case, boundaries, and evidence required before making such a claim.

## Why this matters

KaroX connects an AI client to a selected local Git repository through a guarded, repository-scoped interface. A useful coding-agent request may include development context that a user would not want exposed beyond the minimum required processing boundary:

- source-code fragments and diffs;
- repository structure and file names;
- compiler, test, and runtime errors;
- command and tool output;
- dependency and build information;
- non-secret configuration fragments;
- descriptions of unreleased features or internal implementation details.

KaroX already blocks common secret paths, confines file access to the selected repository, authenticates protected endpoints, exposes explicit access modes, and prevents publishing operations such as `git push`. Those controls protect the local bridge. They do **not**, by themselves, prove that an external inference provider cannot inspect model inputs.

The private-inference research track asks whether KaroX can preserve its normal coding-agent workflow while sending approved context to an inference service that uses confidential-computing controls, such as hardware-backed trusted execution environments and verifiable deployment measurements.

## Concrete scenario

A developer has a private repository containing an unreleased feature. A failing test requires the agent to inspect several files, reason over an error log, propose a patch, apply the change through KaroX, run tests, and report the result.

The intended flow is:

1. The user selects one repository and an explicit KaroX access profile.
2. KaroX creates a repository-scoped session and performs preflight checks.
3. Only task-relevant, policy-allowed context is sent to the selected model endpoint.
4. The model proposes tool calls or code changes.
5. KaroX enforces path, secret, command, branch, and publishing restrictions locally.
6. Tests and Git review are run through guarded endpoints.
7. The final report distinguishes verified results from unverified claims.
8. Audit logs record the workflow while redacting sensitive-looking values.

A confidential-inference provider is useful here because the model still needs repository context, but the user may want stronger assurance about how that context is processed on the provider side.

## Data-flow boundary

```text
Local repository
    ↓ selected, filtered context
KaroX guarded session
    ↓ authenticated API request
Confidential inference endpoint
    ↓ model response / tool proposal
KaroX policy enforcement
    ↓ approved local action
Tests, diff review, guarded commit
```

The private-inference provider does not replace KaroX safeguards. The two layers address different risks:

- **KaroX:** local authorization, repository confinement, secret-path filtering, command policy, Git policy, verification, and auditability.
- **Confidential inference:** reducing provider-side exposure of prompts and outputs, subject to the provider's architecture, attestation model, operational controls, and published limitations.

## Evaluation questions

The first integration should answer:

1. Can the provider be used through a stable API without weakening KaroX authentication or permission boundaries?
2. Does structured output and tool selection remain reliable?
3. What additional latency and failure modes appear?
4. Can the client verify the intended confidential deployment or measurement?
5. Which repository data is sent, retained, logged, or excluded?
6. Can an interrupted or failed confidential request be retried safely without duplicating local actions?
7. Are final completion claims supported by actual test and Git evidence?

## Threat model

The evaluation focuses on:

- accidental exposure of repository context to infrastructure operators or unrelated tenants;
- over-broad context collection by the client;
- prompt injection embedded in repository files;
- model attempts to access blocked files or invoke forbidden commands;
- false claims that tests passed or a change was completed;
- replay or duplicate-action risk after network and tool failures;
- accidental inclusion of sensitive values in logs or support artifacts.

## Non-goals

This work does not claim:

- absolute confidentiality;
- protection from a compromised local machine;
- protection when a repository already contains executable malicious build scripts;
- that confidential computing makes an unsafe agent safe;
- that remote attestation alone proves the entire service is trustworthy;
- that every provider marketed as private has equivalent guarantees.

## Minimum evidence before calling an integration “private”

A provider-specific integration should publish:

- the exact API route and model identifier;
- what client and provider components can see plaintext;
- the confidential-computing technology and attestation flow;
- retention, logging, and support-access behavior;
- a reproducible connectivity and compatibility test;
- latency and error-rate measurements;
- tool-use and structured-output results;
- limitations and unresolved questions.

## Planned public output

The evaluation will publish non-sensitive artifacts only:

- integration instructions;
- a small sandbox benchmark;
- request and response schemas with synthetic data;
- aggregate compatibility, latency, reliability, and cost results;
- a failure taxonomy;
- a clear list of claims that were verified and claims that remain provider assertions.

See [the benchmark plan](../benchmarks/README.md) and [research overview](../RESEARCH.md).