# Contributing to KaroX 5

Thank you for improving KaroX. The project accepts changes that make AI-agent
work more portable, permission-bound, retry-safe, and verifiable without
weakening the local security boundary.

KaroX 5 is under a release feature freeze. Read
[`docs/V5_RELEASE_SCOPE.md`](docs/V5_RELEASE_SCOPE.md) before starting work.

## Classify the change first

Every pull request must choose one class:

- **P0 / release blocker** — security, repository integrity, installation,
  update/rollback, primary ChatGPT/Claude/API flow, false verification, or a
  release gate;
- **P1 / release quality** — onboarding, diagnostics, documentation, evidence
  export, or polish that materially improves the stable scenario;
- **P2 / post-release** — valid work intentionally scheduled after stable 5.0;
- **Deferred/out of scope** — marketplace, default multi-agent orchestration,
  cloud control plane, teams, remote runners, arbitrary website automation, or
  another item deferred by the scope document.

Until stable 5.0, a P2 or deferred change should not be merged merely because it
is implemented. Changing scope requires an explicit rationale in
`docs/V5_RELEASE_SCOPE.md` in the same pull request.

## Development setup

KaroX requires Python 3.10 or newer. Create a virtual environment and install
runtime and development dependencies using the method supported by your current
pip version:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --group dev
```

macOS or Linux:

```bash
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --group dev
```

When the installed pip does not support dependency groups, install the pinned
Ruff, mypy, and coverage ranges from `pyproject.toml` explicitly rather than
inventing different tool versions.

## Required local checks

Run the inexpensive repository contracts first:

```bash
python scripts/check_dependencies.py
python scripts/check_versions.py
python scripts/check_test_count.py
python scripts/check_v5_release.py --json
python scripts/check_release_workflow.py
python -m ruff check src tests scripts
python -m mypy src/karox
```

Run the complete documented suite:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Run coverage without lowering the threshold:

```bash
python -m coverage run -m unittest discover -s tests -p "test_*.py"
python -m coverage combine
python -m coverage report
```

Compile changed Python modules and run focused tests while developing. Before a
release-related pull request, also build and install the wheel outside the
source tree:

```bash
python -m pip wheel . --no-deps --wheel-dir dist
python -m pip install --force-reinstall dist/karox_runtime-*.whl
karox --help
karox-vnext --help
```

The full Windows/macOS/Linux matrix remains authoritative. Do not claim a
platform pass from a different operating system.

## Security invariants

Do not add or merge a path that:

- reads outside the explicitly selected repository;
- follows a symlink or Windows reparse point outside the allowed root;
- exposes credentials in config, logs, session state, evidence, support bundles,
  MCP descriptors, exceptions, or UI;
- allows a hosted client to see every installed tool or MCP server automatically;
- treats authentication as a capability grant;
- executes a mutating call without the required lease and idempotency identity;
- silently retries a mutation with an unknown outcome;
- reports success after a failed, timed-out, or missing verification command;
- enables Git push, remote mutation, or package publishing;
- weakens OAuth redirect, origin, resource, PKCE, refresh rotation, or replay
  checks to work around a client problem;
- describes KaroX as an operating-system sandbox.

A granted process or external MCP server runs with the operating-system rights
of the KaroX process. Document that boundary whenever the user is asked to grant
such a capability.

Security-sensitive changes require a threat-driven regression test and an update
to `SECURITY.md` when the public boundary changes.

## Runtime architecture rules

- Provider adapters and hosted bridge adapters never execute local actions
  directly; they call Core.
- The TUI is a view over shared services, not a second runtime implementation.
- Session mutations use the durable lease and idempotency model.
- Provider, MCP, bridge, and other credential namespaces remain separate.
- No silent fallback may change provider, cost, privacy, or mutation behavior.
- Product-specific compatibility is not called live tested without a dated
  record under `docs/conformance/`.
- A local deterministic fake server proves a contract, not current third-party
  product behavior.

## Tests and evidence

Prefer the smallest test that proves the real boundary:

- unit tests for validation and pure policy;
- real local HTTP/MCP processes for wire behavior;
- subprocess CLI tests for public command paths;
- deterministic idempotency and restart tests for mutations;
- explicit failed-check tests for verification;
- sanitized dated records for live third-party conformance.

Do not increase a published test count merely by splitting one assertion into a
new test method. When the real suite size changes, update every count through the
same commit and let `scripts/check_test_count.py` verify it.

Never commit paid API keys, browser exports, OAuth state databases, raw HAR files,
private source, or unredacted support bundles as evidence.

## Documentation rules

User-facing changes normally require updates to the appropriate canonical files:

- `README.md` and `README_RU.md`;
- `QUICKSTART.md`;
- `docs/CONNECTIVITY.md`;
- `docs/IMPLEMENTATION_STATUS.md`;
- `TROUBLESHOOTING.md`;
- `SECURITY.md`;
- `docs/MIGRATION_V4_TO_V5.md`;
- `docs/conformance/` for live evidence.

The `docs/vNext/` directory is historical. Do not add new shipping instructions
there.

Keep English and Russian instructions equivalent in behavior. Technical command,
protocol, endpoint, schema, and JSON names may remain English.

## Pull request evidence

A pull request should state:

- scope class and release impact;
- user problem and primary scenario affected;
- security/trust boundary affected;
- files and public commands changed;
- focused tests run;
- complete gates run;
- platforms actually exercised;
- limitations and untested external products;
- documentation updated;
- rollback or migration effect.

Do not write “all tests pass” unless the named command was actually run on the
current tree. Distinguish local results from remote CI and live product results.

## Reporting vulnerabilities

Follow `SECURITY.md`. Do not open a public issue containing an exploit,
credential, private repository content, or unsanitized diagnostic archive.
