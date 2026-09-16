# KaroX 5 recovery plan — 2026-08-01

Branch reviewed: `feat/karox-v5-competitive-upgrade`  
Repository: `D:\проекты\KaroX-v5`  
Status: **large incomplete working tree; do not treat as release-ready**

This plan is based on the current local working tree, not the older public GitHub
state. It separates three different kinds of unfinished work:

1. defects observed directly in the current Connections / ClickUp implementation;
2. verification and release blockers already recorded by the repository;
3. missing product behavior that the current UI text promises but the runtime
   does not yet provide.

The goal is not to add more presets. The goal is to make one coherent connection
system that can create, start, inspect, test, stop, restart, edit, and delete both
MCP-client bridges and model-provider connections without lying about their state.

---

## 1. Executive diagnosis

KaroX 5 has a substantial core, but the current branch is not one bug away from
completion. The work is split across four major tracks:

- **Connections architecture:** the new registry, TUI, ClickUp setup, CLI, bridge,
  credential and lifecycle paths are not yet one consistent system.
- **User-facing correctness:** several screens expose controls that cannot work,
  hints describe actions that do not exist, and a saved card is often not an
  active connection.
- **Verification:** the exact allowed test command currently cannot start because
  pytest receives `-n 6` while `pytest-xdist` is not installed in the active
  environment.
- **Release evidence:** live providers, hosted clients, platform installation,
  migration, rollback, artifacts, external beta and final release gates remain
  open.

The correct strategy is therefore:

1. freeze new presets and UI expansion;
2. restore a trustworthy baseline;
3. repair the connection domain model and lifecycle;
4. finish one golden path (ClickUp) end to end;
5. generalize only the parts proven by that path;
6. complete provider management;
7. run release evidence after the product behavior is stable.

---

## 2. Confirmed current blockers

### P0-01 — the configured verification command does not run

Observed command:

```text
C:\Users\ekono\AppData\Local\Programs\Python\Python313\python.exe -m pytest -q
```

Observed failure:

```text
error: unrecognized arguments: -n
inifile: D:\проекты\KaroX-v5\pyproject.toml
```

Cause: `pyproject.toml` injects `-n 6`, but the active Python environment does not
have `pytest-xdist`. This means the working tree cannot currently produce a
credible green or red suite result through the bridge verification allowlist.

Required fix:

- make the development/bootstrap install include the `test` dependency group;
- add an explicit dependency doctor that names missing `pytest-xdist`;
- keep a serial fallback command available for constrained environments;
- align the KaroX bridge verification allowlist with the canonical documented
  runner (`unittest discover`) or ensure the allowed pytest command is always
  self-contained;
- record a fresh baseline only after the command actually starts.

Acceptance:

- the allowed verification command starts without parser/plugin errors;
- the full suite result is recorded with commit, interpreter and platform;
- no documentation claims a passing count before that run.

### P0-02 — automatic tunnel selection is overridden by the ClickUp UI

`resolve_clickup_defaults()` correctly prefers an active tunnel, then Tailscale,
then Cloudflare, then local fallback. However `_ClickupAutoScreen._gather_overrides()`
always writes the currently selected tunnel into the override map. The radio set
starts on Cloudflare, so pressing the automatic-connect button forces Cloudflare
even when the resolver selected Tailscale or local.

Impact:

- the advertised automatic environment detection does not control the actual run;
- a stable Tailscale route can be silently replaced by a temporary Quick Tunnel;
- a machine with no Cloudflare binary may be sent into a guaranteed failure even
  though another valid route was detected.

Required fix:

- represent `auto` as a real UI state rather than preselecting Cloudflare;
- gather only values the user actually changed;
- display resolved values separately from explicit overrides;
- add a TUI test where Tailscale is detected and no user override is made.

Acceptance:

- no interaction in Advanced means the resolver output is passed unchanged;
- selecting an override changes only that field;
- reset restores the original environment-derived result, not hardcoded defaults.

### P0-03 — applying one advanced override destroys other automatic decisions

`apply_clickup_overrides(base, overrides)` does not layer changes onto `base`.
It calls `resolve_clickup_defaults()` again with an empty/unknown environment.
Therefore changing one field can reset:

- the environment-selected tunnel;
- URL stability;
- the previously selected free port;
- tunnel reason/action metadata.

Example: the resolver picks stable Tailscale and port 8766; the user changes only
the endpoint. The override pass can recreate the configuration as Cloudflare on
8765.

Required fix:

- make overrides a pure merge over the resolved base;
- revalidate the merged object without rerunning environment discovery;
- retain provenance per field (`automatic`, `preset`, `user override`);
- add matrix tests for every single-field override over each tunnel choice.

Acceptance:

- an override modifies exactly the named field and any explicitly derived fields;
- untouched fields remain byte-for-byte equivalent to the base decision;
- reset restores the captured base snapshot.

### P0-04 — pasted secret mode is impossible

The Advanced screen can collect `secret`, and the defaults object records only
`secret_source="paste"`. The actual secret value is not carried to the
orchestrator. `setup_clickup_connection()` then sees `paste` and returns
`missing_secret` unconditionally.

Required fix:

- do not put secret values into the serializable defaults dataclass;
- pass a one-shot secret supplier or secret payload separately to the orchestrator;
- consume it once, validate it, store it in the correct credential namespace and
  clear the UI field;
- make generated and supplied credentials follow the same save/cleanup contract;
- add tests that assert the supplied secret is used on the server and handshake,
  but never appears in JSON, logs, progress, errors or evidence.

Acceptance:

- a supplied secret completes setup successfully;
- cancellation/failure deletes only credentials created by the failed attempt;
- no plaintext secret appears in persistent configuration or output.

### P0-05 — Custom tunnel is exposed but cannot be configured

ClickUp Advanced offers `custom`, while `default_tunnel_launcher()` rejects it
unless a public URL is supplied. The ClickUp Advanced form has no custom public
URL field and `ClickupDefaults` has no place to carry one.

Required fix:

- either remove `custom` from ClickUp Advanced until implemented, or implement it
  fully;
- if implemented, require a validated HTTPS origin, endpoint composition and
  stable-URL semantics;
- distinguish "KaroX starts the tunnel" from "user supplies an already-running
  public origin";
- verify host allowlisting and public handshake for the supplied hostname.

Acceptance:

- no visible option ends in a deterministic "cannot be supplied" failure;
- custom URL validation rejects credentials, query, fragment and invalid schemes;
- the saved card and test command use the same final endpoint.

### P0-06 — Advanced transport/auth controls advertise unsupported combinations

The ClickUp screen exposes OpenAPI, API key and custom header. The production
launcher always starts:

```text
karox bridge serve --protocol mcp --profile generic-streamable-http
```

and stores the credential in `KaroX/bridge` for bearer validation. Therefore the
screen can build a target that tests with API-key/custom-header/OpenAPI behavior
against a server that was launched as MCP bearer.

This is configuration fiction: the UI can save a combination the launched runtime
never serves.

Required fix:

- define a central capability matrix for every runtime profile:
  `protocol × transport × auth schemes × tunnel requirements`;
- derive form options from that matrix;
- for the ClickUp preset, expose only the exact known working contract unless a
  second runtime profile truly implements another mode;
- reject unsupported combinations before any secret, session or process is
  created;
- add launcher-contract tests, not only dataclass validation tests.

Acceptance:

- every selectable combination maps to a real launcher configuration;
- the same matrix drives TUI, CLI help, validation and tests;
- no preset-specific UI string can expand runtime capability by itself.

### P0-07 — manual MCP connection forms save metadata but do not make a connection live

For presets other than ClickUp, `_McpClientFormScreen` writes a registry record and
credential. It does not start a bridge, start a tunnel, publish a URL, verify the
wire or persist a managed runtime handle.

The resulting item is a saved recipe, not an active connection. Yet the new docs
call this section "Managing active connections", and the UI offers Test/Copy URL
as though a runtime exists.

Required fix:

- explicitly split domain objects:
  - `SavedConnectionConfig` — desired configuration;
  - `ConnectionRuntimeState` — stopped/starting/running/degraded/failed;
  - `ManagedConnectionHandle` — process/tunnel ownership and stop/restart;
- add Start/Stop/Restart actions;
- show "configured, not started" instead of generic failure when no endpoint is
  known;
- make `test` either start a temporary probe runtime or require a running state;
- rename documentation until active lifecycle exists.

Acceptance:

- a saved record can be clearly distinguished from a running bridge;
- every connection card has an honest state and available actions;
- restart after reopening KaroX is supported or explicitly declared unsupported.

### P0-08 — successful ClickUp process ownership is only an in-memory closure

`ClickupSetupOutcome.stop` can stop the bridge/tunnel only while the result screen
still owns that Python closure. The saved registry record contains no process ID,
tunnel handle, launcher identity or restart metadata.

Consequences:

- after the result card closes, the normal saved-connection list cannot stop the
  live process;
- after KaroX restarts, it cannot reliably adopt, inspect, stop or restart it;
- a child deliberately started in its own process group can outlive the TUI;
- a stale saved card can show an old temporary URL while nothing is listening;
- cleanup depends on the one screen where setup finished.

Required fix:

- move process ownership into a managed runtime registry outside the screen;
- persist safe runtime metadata (not raw process objects): runtime ID, pid,
  creation token, port, tunnel kind, public URL, start time and owner session;
- verify a process before adopting it (PID alone is unsafe due reuse);
- stop managed runtimes on explicit action and define app-exit behavior;
- reconcile stale runtime records at startup;
- expose status/logs/restart from the saved connection list and CLI.

Acceptance:

- closing the result card does not lose lifecycle control;
- reopening the application shows accurate running/stopped status;
- no setup leaves an unmanaged bridge/tunnel after failure, cancellation or
  deliberate stop;
- stale PID records cannot terminate an unrelated process.

### P0-09 — editing an auto-created ClickUp can move the secret to the wrong namespace

The real auto path saves `os-keyring:bridge/<name>`. The generic edit form keeps an
existing secret only if the reference starts with `os-keyring:connection/`.
Editing a ClickUp record with a blank secret therefore creates a new connection
credential while the existing bridge continues validating the old bridge
credential.

Impact: the card, test and running server can disagree immediately after an edit.

Required fix:

- ClickUp edit must use a lifecycle-aware editor, not the generic metadata form;
- preserve the existing credential reference unless rotation is explicitly chosen;
- implement credential rotation as an atomic runtime operation:
  create new secret → update/restart server → handshake → save new reference →
  delete old secret;
- never silently change credential namespaces.

Acceptance:

- editing display metadata does not affect authentication;
- rotating a secret either succeeds end to end or leaves the previous connection
  working;
- tests use the real `bridge` namespace, not only a connection-store stub.

### P0-10 — conditional form visibility is inverted

The manual form initially assigns CSS classes such as `auth-only-custom` and
`tunnel-only-custom`, and CSS hides those classes. `_update_visibility()` calls
`set_class(condition, class_name)`, which adds the hiding class when the field
should be visible and removes it when it should be hidden.

Likely behavior:

- custom-header fields remain hidden when custom header is selected;
- those fields become visible for other auth schemes;
- custom public URL behaves the same way.

Required fix:

- use a positive visible class or toggle `display` directly with correctly named
  helpers;
- apply visibility to labels and inputs together;
- add Pilot tests for each auth/tunnel transition and initial state.

Acceptance:

- only fields required by the selected configuration are visible;
- hidden stale values cannot accidentally affect save;
- keyboard focus skips hidden controls.

### P0-11 — copied ClickUp instructions still name the wrong auth choice

The result card now tells the user to choose ClickUp's exact
`Authentication Method → Authorization header`. However `_clickup_instructions()`
still generates text equivalent to `Select Authentication: Bearer token`.
"Copy instructions" therefore copies different guidance from what the same card
displays.

Required fix:

- one structured instruction builder must feed both visible card and clipboard;
- use the exact ClickUp wording verified in the live form;
- include the temporary-URL warning in copied instructions;
- test the copied value, not only rendered Static widgets.

Acceptance:

- card, clipboard, CLI and docs give identical steps;
- there is no reference to a ClickUp dropdown item that does not exist.

### P0-12 — loopback fallback can produce a false "ready" public connection

A public DNS/network/timeout failure is converted to success when loopback passes.
Loopback proves the local bridge, credential and tools. It does **not** prove that
the public tunnel is reachable by ClickUp. The success detail currently states
that the public URL is reachable from external networks, which is an unsupported
claim.

Required fix:

- represent this as `local_verified_public_pending`, not `ok`;
- keep polling the public endpoint for a bounded propagation window;
- optionally use an independent external probe only with explicit privacy and
  network policy;
- allow the user to copy the URL while clearly showing verification is pending;
- do not persist `ready` until a public probe succeeds or the user explicitly
  accepts an unverified public route.

Acceptance:

- local and public verification are separate fields;
- UI never claims external reachability based solely on loopback;
- ClickUp-ready status requires public evidence or explicit override.

### P0-13 — model-provider screen is not the CRUD surface its own text promises

The module docstring and list hint promise open/edit behavior. Actual screen:

- has no Enter binding or detail screen;
- has no edit action or button;
- `_provider_rows()` computes adapter/model details but `_refresh()` discards
  them and displays only provider IDs;
- providers with multiple models cannot be activated inside the screen and send
  the user to `/models`;
- add exits to a legacy wizard rather than returning to a fully refreshed nested
  flow;
- deletion assumes the keyring account name is `provider_id`, instead of parsing
  and deleting the actual credential reference; environment references need
  different behavior.

Required fix:

- implement provider details and edit;
- add an inline model picker and active-model indicator;
- display adapter, base URL host, model count and credential protection without
  exposing secrets;
- delete by parsed credential reference and never try to delete environment
  variables;
- test provider rename/credential replacement/removal and selected-model repair.

Acceptance:

- every action named in the hint exists;
- a multi-model provider can be activated without leaving Connections;
- deletion removes exactly the intended OS-keyring secret and no other account.

### P0-14 — credential naming can collide and old secrets can be orphaned

The manual MCP form derives the keyring account from
`name.lower().replace(" ", "-")`. Different names can normalize to the same
account, and renaming/replacing a secret does not reliably delete the old account.
The display name should never be the credential identity.

Required fix:

- derive credential account from immutable `connection_id`;
- keep display-name changes independent;
- rotate through an explicit transaction;
- add a credential garbage-collection/doctor report for unreferenced KaroX-owned
  entries where the backend permits enumeration, otherwise track safe tombstones;
- test normalization collisions and rename paths.

Acceptance:

- two connections with equal/similar names cannot overwrite each other's secret;
- rename creates no new credential;
- successful rotation removes the previous secret only after the new runtime is
  proven.

### P0-15 — tests overuse stubs that differ from production ownership

Examples in the current tests:

- TUI ClickUp success stores a secret in `KaroX/connection`, while production
  stores it in `KaroX/bridge`;
- UI tests assert that a card appears, but not that the launched profile, auth,
  protocol, endpoint and copied instructions agree;
- resolver tests verify base selection and one port override, but not preservation
  of the environment-selected base through arbitrary overrides;
- there are no tests for inverted conditional visibility;
- provider list tests only assert one option exists;
- lifecycle tests hold the stop closure directly and do not test losing/reloading
  it from the saved registry.

Required fix:

- keep fast unit seams, but add contract tests against production-shaped records;
- add a scenario test for each user journey rather than only widget presence;
- assert negative behavior: unsupported controls are absent, secrets are not
  leaked, stale URLs are not reported as ready, and restart state reconciles.

Acceptance:

- the golden ClickUp scenario uses the real bridge credential namespace;
- a test fails for every P0 defect listed above before its fix;
- no release claim relies solely on mocked launchers.

---

## 3. P1 correctness and product debt

### P1-01 — `/connect`, `/connections`, docs and module descriptions disagree

Current behavior keeps `/connect` on the legacy onboarding wizard and puts the new
hub on `/connections`. Some module text says the new screens live behind
`/connect`; hints and welcome text still direct unconfigured users to `/connect`.
This may be an intentional compatibility split, but it is not presented as one
coherent product model.

Decision required:

- make `/connect` the universal hub and move the old choice into a hub action; or
- keep both, but rename and document them clearly (`/setup` vs `/connections`).

### P1-02 — remaining TUI defects

`docs/UX_BUG_INVENTORY.md` records two open P1 items:

- UX-006: no way to hand mouse selection back to the terminal;
- UX-010: a 14-row window leaves only three conversation rows.

These remain release-relevant usability work.

### P1-03 — canonical status documents contradict each other

`docs/IMPLEMENTATION_STATUS.md` says the terminal inventory has four P0 defects
from the old RichLog design. `docs/UX_BUG_INVENTORY.md` says thirteen of fifteen
are closed and only two P1 defects remain. The implementation status is stale.

The README also says the suite is 876 tests, then retains a sentence claiming 790
are collected under `tests`. The automated claim checker does not match that
second explanatory sentence, so it can remain stale while the gate passes.

Required fix:

- generate or validate status summaries from canonical inventories;
- broaden documentation contracts to cover explanatory count claims;
- never manually duplicate mutable counts where a generated table can be used.

### P1-04 — `connections` CLI is incomplete lifecycle management

Current CLI supports list/show/test/remove. Missing operations include:

- create/import;
- edit;
- start;
- stop;
- restart;
- status/logs;
- rotate secret;
- validate without network;
- export a secret-free support record.

Until these exist, describe it as registry inspection, not active connection
management.

### P1-05 — unsafe or misleading secret reveal workflow

`connections show --reveal-secret` prints a full credential to stdout. That is
sometimes necessary, but it is easy to capture in shell history, CI logs,
terminal recording or redirected files.

Required hardening:

- require an interactive confirmation when stdout is a TTY;
- refuse or require an extra flag when stdout is redirected;
- prefer clipboard/one-time reveal where available;
- add a conspicuous audit event that does not contain the value;
- ensure JSON reveal is never enabled accidentally by a generic debug command.

### P1-06 — error classification conflates DNS and connection refusal

`_classify_request_error()` maps every `httpx.ConnectError` to `dns_failure`, even
though connect refused, no route, proxy failure and DNS failure are different
remediations. The MCP ExceptionGroup path has richer string matching than the
plain HTTP path.

Required fix:

- classify using exception causes (`socket.gaierror`, connection refused, TLS,
  proxy, route);
- use one classifier for MCP and OpenAPI/provider paths;
- keep safe, redacted technical details in diagnostics.

### P1-07 — OpenAPI endpoint composition is fragile

The saved target can already contain `/openapi.json`, while `_test_openapi_wire()`
strips the supplied endpoint and appends `/openapi.json` and `/tools`. This can
create paths such as `/openapi.json/openapi.json` depending on what the card stores.

Required fix:

- model an origin/base URL separately from protocol endpoints;
- centralize endpoint construction;
- never infer an origin by string trimming an arbitrary endpoint path.

### P1-08 — no connection health model

A single handshake result is not enough for a durable card. Missing state includes:

- last tested time;
- local/public test results separately;
- running process/tunnel status;
- credential availability/fingerprint match;
- URL age/stability;
- tool-set fingerprint;
- last error/remediation;
- stale configuration detection.

Add a health snapshot that is safe to persist and display.

### P1-09 — no permission/tool-selection step in automatic ClickUp

The automatic path grants a fixed read-only set, while the saved session itself is
created as `WORKSPACE_WRITE`. This is confusing and makes the effective permission
boundary less obvious.

Required fix:

- show the effective profile and exposed tool list before start;
- create the least-privileged session that matches the exposed tools;
- allow an explicit upgrade through the same approval system used elsewhere;
- record selected tools and profile on the connection card.

### P1-10 — current working tree is too broad for safe review

The branch mixes:

- connection domain and TUI;
- ClickUp orchestration;
- CLI secret inspection;
- MCP wire-name rewriting;
- bearer parsing changes;
- stdout encoding changes;
- docs/test-count updates;
- unrelated Windows test accommodation.

Split into reviewable commits after the baseline is restored. Do not continue
adding behavior to one unverified diff.

---

## 4. Release blockers already recorded by the repository

These are not all code defects, but they prevent a truthful stable 5.0 release.

### Verification and security

- full current suite not recorded on the current tree;
- Ruff, Mypy and coverage not recorded on the current tree;
- dependency/version/product/profile/workflow gates not rerun;
- KB-HYBRID records not regenerated on the candidate;
- traversal, symlink/reparse, secret reflection, hosted allowlist, concurrency,
  lease, idempotency and verification invariants need exact-candidate evidence;
- wheel install and CLI smoke test are pending.

### Platform and artifact matrix

- GitHub Actions acceptance of the updated workflow is pending;
- Windows/macOS/Linux Python matrices are pending;
- separate `karox-remote` artifact build/install is pending;
- PowerShell 5.1 and POSIX launcher checks are pending;
- source archives/checksums/release-order evidence is pending.

### Installation and lifecycle

For all three OS families:

- clean install;
- reinstall;
- 4.x → 5 migration;
- interrupted update rollback;
- failed validation rollback;
- uninstall without repository deletion;
- session vs credential cleanup;
- previous stable launcher rollback window.

### Live product conformance

Pending real-account records:

- ChatGPT Web;
- Claude Web;
- OpenAI Responses;
- Anthropic Messages;
- Gemini;
- one generic OpenAI-compatible provider;
- Ellipsis account-specific interactive contract and paid bounded acceptance.

### External beta

Pending:

- candidate recruitment;
- five unaided installs;
- three primary-scenario completions;
- two unaided completions;
- two voluntary second uses;
- threshold evidence and P0 closure.

### Final release content

Pending:

- current demo;
- clean-machine quick start;
- final security/troubleshooting review;
- changelog;
- release notes and breaking changes;
- known limitations;
- published artifact and stable update-channel verification.

---

## 5. Target architecture before more presets

### 5.1 Domain objects

Introduce explicit objects instead of one `McpClientTarget` carrying desired and
observed state together:

```text
ConnectionPreset
  declarative defaults and supported capability matrix

SavedConnectionConfig
  immutable ID, display name, desired transport/auth/tunnel/profile/tools,
  endpoint origin/path, credential reference, user overrides

ConnectionRuntimeRecord
  runtime ID, config ID, process identity, local endpoint, public origin,
  tunnel identity, status, timestamps, safe diagnostics

ConnectionHealthSnapshot
  local handshake, public handshake, credential status, tool fingerprint,
  last failure and remediation
```

### 5.2 Services

```text
ConnectionRegistry
  atomic config CRUD and schema migration

ConnectionCredentialManager
  immutable credential identity, rotation transactions, namespace dispatch

ConnectionResolver
  environment probe + preset defaults + explicit overrides + provenance

ConnectionLauncher
  converts validated config to one real runtime contract

ConnectionRuntimeManager
  start/stop/restart/adopt/reconcile/logs

ConnectionVerifier
  local and public tests with accurate classification

ConnectionController
  the only API called by CLI and TUI
```

The TUI must not import launch internals, keyring stores and registries separately.
It should call the controller and render returned state.

### 5.3 Capability matrix

For every runtime profile, encode:

- protocols served;
- supported auth schemes;
- allowed tunnel kinds;
- stable URL requirement;
- credential namespace;
- required launcher arguments;
- available permission profiles/tools;
- whether the public endpoint can be verified locally;
- supported external clients and evidence status.

Forms and CLI choices must be generated from this matrix. A UI control that is not
backed by the matrix must not exist.

### 5.4 Lifecycle policy

Decide and document:

- whether bridges stop when the TUI exits;
- whether they can run as managed background services;
- who owns a tunnel;
- how stale processes are reconciled;
- how temporary URLs are invalidated on restart;
- how a saved connection restarts with the same credential;
- when sessions and credentials are deleted;
- how logs are retained and redacted.

Do not leave this behavior inside screen callbacks.

---

## 6. Execution plan

## Phase 0 — freeze and recover the baseline

Tasks:

- stop adding presets, fields and documentation claims;
- snapshot the current diff and split unrelated changes conceptually;
- fix/bootstrap test dependencies;
- run the canonical suite and static gates;
- record every current failure before changing behavior;
- correct stale test-count/status documentation only from actual discovery;
- add regression tests for P0-02 through P0-15, initially failing where needed.

Exit gate:

- verification commands run;
- baseline record exists;
- no unknown red suite state;
- every confirmed defect has a test or explicit manual reproduction.

## Phase 1 — correct the connection model and validation

Tasks:

- add immutable credential identity based on connection ID;
- separate origin, endpoint path and effective endpoint;
- implement capability matrix;
- replace re-resolution override logic with a true merge/provenance model;
- remove unsupported ClickUp choices;
- correct conditional visibility;
- add schema migration from current `connections.json`;
- make docs call saved records "configured" until runtime state exists.

Exit gate:

- invalid combinations cannot be constructed by TUI, CLI or direct model load;
- override matrix tests pass;
- old registry data migrates without exposing or losing credentials.

## Phase 2 — implement managed runtime lifecycle

Tasks:

- create `ConnectionRuntimeManager`;
- move bridge/tunnel ownership out of `ClickupSetupOutcome`;
- persist safe runtime records;
- implement start/stop/restart/status/logs;
- reconcile stale records on startup;
- implement app-exit behavior;
- make operations idempotent and retry-safe;
- ensure deletion stops runtime before credential/config removal, with rollback on
  partial failure.

Exit gate:

- closing/reopening TUI retains accurate control;
- no unmanaged child remains after failure/cancel/stop;
- saved stopped connections restart successfully;
- stale PID protection is tested.

## Phase 3 — finish ClickUp as the golden path

Tasks:

- auto resolver uses real environment result without accidental overrides;
- exact supported auth/protocol only;
- generated and supplied secret flows;
- explicit permission/tool review;
- local start and public tunnel start;
- separate local/public verification states;
- exact ClickUp `Authorization header` instructions everywhere;
- temporary URL lifecycle and restart warning;
- saved card with Start/Stop/Restart/Test/Rotate/Delete;
- live ClickUp conformance record with sanitized evidence.

Exit gate:

- new user can select ClickUp, press one primary action, paste URL/secret into the
  verified ClickUp fields and complete a real tool call;
- restarting KaroX produces an understandable state and recovery action;
- copied instructions and UI are identical;
- no mocked result is used as release evidence.

## Phase 4 — generalize MCP client connections

Tasks:

- generic Streamable HTTP bearer path;
- local IDE path;
- Notion/PromptQL paths only where capability and live evidence exist;
- OAuth web clients use their separate stable-URL flow;
- custom path exposes only runtime-supported fields;
- per-connection tool/profile selection;
- common start/stop/test/health behavior;
- remove preset-specific branches from screens after controller generalization.

Exit gate:

- presets are metadata over one controller path;
- custom connection can be created and run without editing source;
- unsupported profiles are labelled Experimental and cannot claim live success.

## Phase 5 — complete model-provider management

Tasks:

- provider detail/edit screen;
- base URL and adapter validation;
- model fetch/manual model flow;
- inline model selection and active state;
- capability editing (tools/streaming/vision/reasoning) with source/probe labels;
- real minimal test with budget/timeout guard;
- correct credential reference deletion/rotation;
- provider health and last-test snapshot;
- CLI/TUI parity.

Exit gate:

- add/edit/test/select/delete all work inside Connections;
- multiple models are manageable without `/models` escape hatch;
- provider credentials never appear in JSON/log/evidence;
- four named provider live records are completed later in Phase 8.

## Phase 6 — CLI/TUI/docs consistency

Tasks:

- choose final `/connect`/`/connections` semantics;
- expose controller operations in CLI;
- correct all hints and module docstrings;
- make CLI and TUI show the same state vocabulary;
- harden secret reveal/copy;
- generate help and documentation contracts from parser/capability data where
  practical;
- fix UX-006 and UX-010;
- update canonical status/inventory/count documents.

Exit gate:

- every advertised action exists;
- every public command is exercised against installed-wheel `--help`;
- status documents do not contradict inventories or discovery.

## Phase 7 — security and failure hardening

Tasks:

- credential rotation transaction tests;
- config/runtime/delete partial-failure recovery;
- process identity/adoption safety;
- host allowlist and tunnel-host changes;
- accurate DNS/connect/TLS/proxy classification;
- public verification honesty;
- path/repository/session permission review;
- no-auth removal or explicit local-only isolation;
- support bundle coverage for connection failures without source/secrets;
- fuzz malformed registry records and endpoint values.

Exit gate:

- security review passes on exact candidate;
- failure cannot produce a false ready state;
- retry cannot duplicate process, tunnel, secret or registry mutations.

## Phase 8 — full release evidence

Tasks:

- complete suite, Ruff, Mypy, coverage and repository gates;
- wheel + separate remote artifact build/install/smoke;
- Windows/macOS/Linux matrix;
- clean install/migration/update rollback/uninstall;
- ChatGPT and Claude live conformance;
- provider live conformance;
- Ellipsis bounded live acceptance;
- external beta thresholds;
- final docs/demo/changelog/release notes;
- strict release gate and artifact-before-tag workflow.

Exit gate:

- every unchecked P0 release item has reproducible dated evidence;
- `python scripts/check_v5_release.py --strict` passes on the exact artifact-tested
  tree;
- final version transition happens only after that result.

---

## 7. Recommended commit sequence

Do not land the whole current working tree as one commit. Suggested sequence:

1. `test: restore deterministic local verification bootstrap`
2. `test: codify current connections and clickup failures`
3. `refactor: introduce connection capability and resolved-config model`
4. `fix: preserve automatic clickup decisions and valid advanced overrides`
5. `refactor: move connection process ownership into runtime manager`
6. `fix: make clickup credential and lifecycle paths atomic`
7. `feat: complete clickup golden-path UI and CLI lifecycle`
8. `feat: generalize managed MCP client connections`
9. `feat: complete model-provider CRUD and model selection`
10. `fix: align commands, hints, docs and status contracts`
11. `test: add installed-artifact and lifecycle scenario coverage`
12. `docs: record live conformance and release evidence`

Each commit should have focused tests and no unrelated documentation count bump
unless discovery actually changed.

---

## 8. Immediate next tasks

Start with these in order:

1. make the allowed test command executable by installing/declaring xdist or
   providing an allowed serial runner;
2. add failing tests proving automatic Tailscale is overridden by the untouched
   Advanced UI;
3. add failing tests proving one override destroys base tunnel/port decisions;
4. add failing tests for pasted-secret and custom-tunnel impossibility;
5. remove unsupported ClickUp OpenAPI/API-key/custom-header choices;
6. fix inverted manual-form visibility and test keyboard focus;
7. introduce immutable credential IDs and preserve bridge references on edit;
8. design and implement a managed runtime record before adding more Start buttons;
9. change loopback fallback from `ok` to `public_pending`;
10. correct copied ClickUp instructions and the stale canonical docs;
11. rerun the full baseline;
12. only then continue provider CRUD or additional presets.

---

## 9. Definition of done for Connections

Connections is complete only when all statements below are true:

- a user can distinguish configured, starting, running, degraded, stopped and
  failed states;
- every selectable combination maps to a real runtime contract;
- automatic values are automatic, and overrides change only what the user chose;
- secrets have immutable identities, safe rotation and correct cleanup;
- closing/reopening KaroX does not lose lifecycle control;
- local verification is never presented as public verification;
- TUI and CLI use the same controller and state vocabulary;
- every action shown in hints/buttons/help is implemented;
- saved MCP clients can start/stop/restart/test/delete;
- model providers can add/edit/test/select/delete, including multi-model choice;
- exact ClickUp instructions are proven against a real workspace;
- failure/cancel/retry cannot leak processes, tunnels, sessions or credentials;
- full deterministic and live evidence exists for every stable claim.

Until then, the feature should remain Preview/Experimental and the stable 5.0
release decision must remain **NOT READY**.

---

## 10. Implementation progress — 2026-08-01

Implemented in the current working tree after this plan was written:

- the canonical pytest command no longer requires an undeclared xdist plugin;
- ClickUp Advanced keeps the environment-resolved tunnel, port and URL stability;
- one override changes only its own field;
- supplied secrets travel separately from serializable override metadata;
- unsupported ClickUp OpenAPI/API-key/custom-header/custom-tunnel choices are no
  longer offered by the automatic flow and are rejected by the orchestrator;
- manual-form conditional fields now appear for the selected condition rather
  than the inverse;
- new MCP connection credentials use immutable connection IDs, and generic edit
  cannot silently rotate a live bridge credential into another namespace;
- local-only verification is reported as `public_pending`, not public success;
- copied ClickUp instructions use the exact `Authorization header` choice and
  include temporary-URL behavior;
- provider deletion follows the actual credential reference instead of assuming
  the provider ID is the keyring account;
- a safe connection runtime registry now separates configured connections from
  running, degraded, unmanaged-running and stopped runtimes;
- ClickUp registers bridge/tunnel ownership outside the result card;
- TUI and CLI expose runtime status and managed stop;
- connection removal stops a managed runtime first and refuses to delete an
  unmanaged live process by PID alone;
- current documentation claims were synchronized to discovery: 889 tests under
  `tests`, 894 at repository root including five legacy checks.

Verification completed:

- focused Connections/ClickUp/provider/CLI/TUI/runtime/release-gate suite: **93 passed**;
- real local MCP bridge initialize + tools/list path is included in that run;
- test collection completed successfully and reported **889 tests**.

Not completed by this implementation slice:

- safe cross-restart process adoption or termination of `unmanaged_running`
  records (PID reuse prevents doing this without stronger identity evidence);
- Start/Restart of a stopped saved MCP runtime;
- full model-provider edit/details/multi-model picker;
- full 889-test execution within the bridge's 300-second verification window;
- platform, install/migration/rollback, live third-party and external-beta
  evidence from the release checklist.
