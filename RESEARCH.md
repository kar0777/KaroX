# KaroX research overview

KaroX is an open-source bridge that exposes a selected local Git repository to compatible AI clients through a guarded API and MCP workflow. The project is also a practical test environment for studying the reliability and safety of tool-using coding agents.

## Current engineering capabilities

The current public release includes:

- repository-scoped sessions;
- Observe, Build, Resume, and Advanced access profiles;
- protected API and MCP endpoints;
- repository path confinement and sensitive-file filtering;
- command restrictions and a hard no-push policy;
- guarded local commits;
- preflight, diagnostics, status, and Control Center visibility;
- redacted bounded audit logs and source-free support bundles;
- Windows, macOS, and Linux support;
- CI and CodeQL workflows.

These are product controls implemented by KaroX. They should not be confused with guarantees about the confidentiality, correctness, or safety of a connected model provider.

## Research direction

The initial study focuses on four questions:

1. **Permission compliance:** does the agent stay within the access profile and user-approved scope?
2. **Verification:** does the agent test its changes and distinguish evidence from assumptions?
3. **Failure recovery:** can it recover from failed or malformed tool calls without duplicating side effects?
4. **Private inference compatibility:** can a confidential-inference endpoint process approved repository context without weakening KaroX safeguards or agent usability?

## Proposed methodology

A reproducible synthetic-repository benchmark will compare configurations using identical tasks, prompts, KaroX versions, and repository commits. Each run will record tool calls, changed files, tests, Git state, verification evidence, latency, token usage, and cost where available.

The first benchmark covers ten classes of tasks, including bug fixes, multi-file changes, failure recovery, approval gates, blocked secret access, repository prompt injection, destructive-command prevention, and guarded local commits. See [`benchmarks/README.md`](benchmarks/README.md).

## Private inference

Coding agents may require source fragments, repository structure, build errors, test output, and descriptions of unreleased implementation details. KaroX can restrict what leaves the local repository, but a separate provider-side question remains: how is approved context processed after it reaches the inference service?

The confidential-inference track evaluates providers that claim hardware-backed protected execution or comparable controls. It will document the API route, plaintext boundary, attestation flow, logging and retention behavior, compatibility, latency, reliability, and limitations. No provider will be described as private solely because of marketing language.

See [`docs/private-inference-use-case.md`](docs/private-inference-use-case.md).

## Planned artifacts

- synthetic benchmark fixtures and task prompts;
- per-run result records;
- sanitized logs and diffs;
- aggregate success, violation, recovery, and verification rates;
- provider integration notes;
- a failure taxonomy;
- explicit limitations and unanswered questions.

## Research integrity

- No real personal, customer, or production data will be included in public tests.
- No API keys, session keys, tunnel credentials, or secrets will be published.
- Planned work will be labeled as planned; measured results will be labeled as measured.
- Provider claims will be separated from independently verified properties.
- Failed runs and negative results will be reported rather than discarded.

## Contact and project

- Repository: https://github.com/kar0777/KaroX
- Maintainer profile: https://github.com/kar0777
