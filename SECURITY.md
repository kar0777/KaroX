# KaroX Security Policy

KaroX exposes a selected local Git repository to compatible AI clients through guarded local tools, MCP, and provider integrations. It reduces authority with repository confinement, explicit capabilities, secret handling, leases, idempotency, and evidence. It is not an operating-system sandbox: an approved local process still runs with the OS rights of the KaroX user.

## Supported versions

KaroX 5 is currently a public pre-release (`5.0.0rc1`). Security fixes for KaroX 5 are developed and validated on the current release tree and then promoted to `main`. The stable 4.x channel remains separate until the 5.0 stable evidence gates are complete.

## Reporting a vulnerability

Do **not** publish credentials, exploitable details, or private-repository data in a public issue. Contact the repository maintainer privately through the repository owner's GitHub contact channel and include only the minimum sanitized reproduction needed to understand the issue.

Useful details are: KaroX version, operating system, access profile, affected tool/endpoint, expected result, actual result, and a reproduction using synthetic data. Never include live bridge credentials, provider keys, tunnel credentials, cookies, passwords, or private source.

## Credential handling

KaroX stores provider and bridge credentials in the operating-system keyring where the supported flow provides one. Credentials are filtered/redacted at hosted process and tool boundaries and are not intended to be persisted in normal JSON configuration, task state, Git-tracked files, wheel contents, or public logs.

The bridge authorization-key copy flow uses short-lived clipboard delivery rather than returning the secret in ordinary model-visible output. Any explicit secret-reveal path remains separately confirmed.

## Local security boundary

KaroX is designed around these controls:

- repository reads and writes are confined to the selected repository root;
- path traversal and common credential/secret paths are blocked;
- high-risk mutations are separated from ordinary development work;
- retries of mutating operations use idempotency and durable task state;
- cross-process repository mutation uses leases/fencing;
- verification results, Git status, Git diff, and artifacts provide evidence instead of relying on model narration;
- browser and desktop automation are bounded by explicit capabilities and user takeover for login/CAPTCHA/2FA-sensitive interactions;
- support/evidence surfaces redact sensitive-looking values.

Filtering reduces risk but cannot recognize every application-specific secret. Do not use real production/customer secrets in repositories prepared for agent experiments.

## Access profiles and external effects

KaroX exposes three stable access profiles:

- **Observe** (`read_only`) — repository/Git inspection plus `browser.read` for non-mutating browser observation; it does not grant `browser.input`.
- **Build** (`workspace_write`) — repository mutation, approved checks/processes, selected MCP actions, `browser.read`, and bounded `browser.input`. Build does not grant `git.commit`.
- **Advanced** (`elevated`) — Build capabilities plus guarded local `git.commit`, desktop input, network access, and other explicitly elevated local developer actions.

Normal reads, edits, checks, local development commands, and permitted local commits should not require nuisance confirmation loops. Protected destructive work can still stop at a meaningful boundary.

`git push`, force-push, package publishing, deployment, authentication changes, and release publication are **not standing capabilities of any stable access profile**. They require dedicated guarded surfaces. Normal push is exact-action and one-shot; force-push never inherits that approval. The KaroX 5 pre-release publisher likewise validates the exact tag, commit, successful CI evidence, configured GitHub remote, and a fresh exact user approval before creating the remote pre-release tag.

For MCP clients with native elicitation, that protocol is the strongest model-independent confirmation path. ChatGPT sessions may also use KaroX's explicit chat-native confirmation compatibility path: a fresh `yes` is bound to one exact action in the active workstream, consumed once, and cannot be replayed for a second push or publication.

## Command and build risk

A repository may contain executable scripts in package manifests, Makefiles, shell scripts, test hooks, build systems, or generated tooling. Running an approved build/test command can execute those scripts with the KaroX user's OS privileges.

Before granting Build or Advanced access to an unfamiliar repository:

1. inspect it in Observe mode;
2. review build, test, install, and hook scripts;
3. use a disposable branch/environment where practical;
4. keep personal/production/customer data out of the workspace;
5. review the final diff and verification evidence before accepting a commit or external effect.

## Model-provider boundary

KaroX controls the local bridge; it does **not** by itself prove that a connected model provider offers confidential inference, zero retention, trusted execution, or protection from provider operators.

When approved repository context is sent to an external model endpoint, evaluate separately:

- exactly what data leaves the local machine;
- prompt/output logging and retention;
- who can access plaintext;
- whether confidential-computing or attestation claims are independently verifiable;
- authentication, retry, and failure behavior;
- provider terms and account-owner requirements.

See [the private-inference use case](docs/private-inference-use-case.md) and [research overview](RESEARCH.md) for the evidence standard KaroX uses before making provider-side privacy claims.

## Threat model summary

| Asset | Example threat | KaroX mitigation |
| --- | --- | --- |
| Bridge credential | Leak in normal output/logs | keyring-backed flows, secret filtering, redaction |
| Provider API key | Exposure through config or argv | guarded credential storage/injection and output filtering |
| Public bridge URL | Unauthorized tool calls | authenticated bridge/session boundary |
| Workspace files | Accidental/destructive mutation | repository confinement, access profiles, transactions/checkpoints, evidence |
| Remote Git | Unintended push/rewrite | dedicated exact-action push; force-push never implied |
| Pre-release publication | Wrong commit/tag published | exact tag/version/CI validation plus one-shot approval and remote reconciliation |
| Browser session | Login/payment/2FA action without user | bounded input policy and explicit user takeover |

## What KaroX cannot guarantee

KaroX cannot fully protect against a compromised local machine/user account, malicious code already present in an approved repository, a user intentionally granting excessive authority, unknown secret formats, provider-side behavior outside the local KaroX boundary, or logic errors in agent-generated code.

KaroX also does not bypass CAPTCHA or 2FA, disable TLS verification, silently authenticate to external services, or turn a failed verification step into success.
