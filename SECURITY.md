# KaroX security model

KaroX exposes a selected local Git repository to compatible AI clients through a protected API and MCP workflow. This is a powerful development interface and should be treated as a guarded tool with explicit boundaries, not as an absolute sandbox.

## Protected by default

KaroX is designed to enforce the following local controls:

- every protected endpoint requires the session-specific credential;
- file access is confined to the selected repository root;
- path traversal is blocked;
- common secret and credential paths are filtered;
- Observe mode remains read-only;
- dangerous system commands and publishing operations are blocked;
- `git push` and remote modification are not allowed;
- local commits are created only through the guarded commit endpoint;
- request sizes and repeated authentication failures are bounded;
- audit logs rotate and sensitive-looking values are redacted;
- generated support bundles exclude source code and perform a final secret scan.

The protected credential must be entered only into the compatible client's secure credential field. It must not be pasted into chat, a public issue, a screenshot, or a repository file.

## Sensitive paths

The runtime blocks common categories such as:

- `.env` and related environment files;
- SSH keys and SSH configuration containing credentials;
- token, cookie, and credential files;
- private-key formats such as `.pem`, `.key`, `.p12`, and `.pfx`;
- paths outside the selected repository.

This filtering reduces risk but cannot identify every application-specific secret. Do not store real secrets in repositories used for agent experiments.

## Command and build risk

A repository may contain executable scripts in `package.json`, Gradle files, Makefiles, shell scripts, test hooks, or other build configuration. Running an approved build or test command can execute those scripts.

Before using Build or Advanced mode with an unfamiliar repository:

1. inspect it in Observe mode;
2. review build and test scripts;
3. use an isolated branch and disposable environment;
4. confirm that no personal, production, or customer data is present;
5. review the diff and test evidence before accepting a commit.

## Model-provider boundary

KaroX controls the local bridge. It does not automatically guarantee that a connected model provider offers confidential inference, zero retention, trusted execution, or protection from provider operators.

When repository context is sent to an external model endpoint, evaluate separately:

- what data leaves the local machine;
- whether prompts and outputs are logged or retained;
- who can access plaintext;
- whether confidential-computing claims are independently verifiable;
- how authentication, retries, and failures are handled;
- whether the provider's terms permit the intended data and account owner.

See [the private-inference use case](docs/private-inference-use-case.md) for the planned evaluation methodology.

## What KaroX cannot guarantee

KaroX cannot fully protect against:

- a compromised local machine or user account;
- malicious code already present in an approved repository;
- a user intentionally granting excessive permissions;
- an AI client that leaks the credential or ignores its own security controls;
- provider-side behavior outside KaroX's technical boundary;
- all possible secret formats or sensitive business information;
- logic errors in agent-generated code.

## Operational recommendations

- Start unfamiliar projects in Observe mode.
- Prefer Build mode and an isolated branch for ordinary changes.
- Use Advanced mode only when the broader command set is necessary.
- Run `karox doctor` before important sessions.
- Use `V` preflight before connecting an agent.
- Review changed files, tests, and Git status before accepting completion.
- Stop the session when work is finished.
- Never publish a live tunnel URL together with its session key.
- Review a support bundle before sharing it privately with maintainers.

## Reporting a vulnerability

Do not publish credentials, exploitable details, or private repository data in a public issue. Report the minimum reproducible information privately to the maintainer through the repository owner's GitHub contact channel. Include the KaroX version, platform, access profile, affected endpoint or command, and a sanitized reproduction.
