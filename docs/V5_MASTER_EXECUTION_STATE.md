# KaroX 5 master execution state

Last updated: 2026-08-02  
Branch: `feat/karox-v5-competitive-upgrade`  
Repository: `D:\проекты\KaroX-v5`  
Release decision: **NOT READY**

This page records the current multi-agent working-tree baseline. It is not a
release claim and does not replace dated conformance records.

## 1. Working-tree rule

The branch contains broad uncommitted work from several agents. Preserve
unrelated changes. Do not reset, clean, commit, push, publish, deploy, or rewrite
history without an explicit instruction from Egor.

Known temporary recovery artifacts were removed earlier. The Connections,
browser, hot-worker, process-identity, and provider-controller modules are active
implementation, not cleanup candidates.

## 2. Canonical deterministic baseline

Current discovery:

- suite under `tests`: **1015 tests**;
- repository-root collection: **1020 tests**;
- difference: five legacy script checks under `scripts/`.

`tests/test_release_gates.py`: **10 passed** after synchronizing the published
counts in README, README_RU, implementation status, release checklist, and the
archived vNext index.

Latest complete four-way deterministic pytest baseline:

| Split | Result |
| --- | --- |
| 1/4 | 267 passed, 2 skipped, 45 subtests |
| 2/4 | 196 passed, 2 skipped, 49 subtests |
| 3/4 | 401 passed, 1 xfailed, 91 subtests |
| 4/4 | 148 passed, 63 subtests |

No split failed. The two split parts that previously emitted the MCP SDK
warning were repeated after moving all probes and wire helpers to the modern
`streamable_http_client`; both stayed green and emitted no warnings. The shared
transport keeps a compatibility fallback for older MCP SDK installations.
After the split run, one existing provider-controller test was strengthened to
verify transaction rollback; that focused file remains green: **5 passed**.

## 3. Hosted developer session hardening — complete

### Mutation leases

Hosted mutations no longer hold a lease for the remote request deadline.

- lease TTL: 60 seconds;
- heartbeat: every 20 seconds while the command is running;
- heartbeat failure fails closed;
- normal completion stops the heartbeat and releases the lease;
- a killed/disconnected client can block the next mutation only until the short
  stale lease expires, not for a 15-minute ClickUp request window.

### Durable saved bridge identity

A saved web-bridge profile is now a durable connector identity:

- deterministic session ID derived only from the saved profile name;
- automatic compatibility reuse of identities created by the earlier
  target-profile + name scheme;
- repository/access-profile validation on reuse;
- stable secret in the OS keyring across ordinary launcher restarts;
- saved session, OAuth registration/grants, and secret survive `Ctrl+C`;
- orphan reaping kills proven process trees but preserves durable session auth;
- failed first launch removes only the never-activated credential;
- profile deletion refuses a live launcher and then removes current/legacy
  sessions plus keyring credentials;
- repository/access-profile edits require explicit `--reset-identity`, because
  that operation intentionally rotates the connector secret;
- diagnostics report `persists across managed launcher restarts`.

`karox bridge saved ensure-developer NAME --repository PATH` now creates or
idempotently updates the complete Tailscale workspace developer profile with
repository mutation tools, structured tests, Ruff, Mypy, wheel build, and the
safe dev-server profile. Repeating it with the same binding does not rotate the
connector identity.

A saved Tailscale profile therefore keeps both the `.ts.net` URL and its auth
state after an ordinary restart. Focused saved-profile/launcher/release evidence:
**54 passed, 1 skipped, 15 subtests**.

### Tool contract normalization

For write-capable profiles:

- repository write tools imply `karox.repo.command`;
- any approved verification-command allowlist publishes both
  `karox.checks.run` and `karox.tests.run`;
- browser families imply `karox.browser.command`;
- the short `karox connect ... --write` alias supplies the same safe server
  profile as the long `bridge connect` path.

The currently running bridge was launched before the final automatic
`checks.run` exposure change. It must be restarted through the saved profile
before Ruff, Mypy, and wheel commands can be called through that connector.
The bridge was intentionally not restarted while Egor was away because doing so
would disconnect the active control session.

## 4. Strong runtime identity — complete

`src/karox/process_identity.py` and runtime registry schema v2 provide:

- OS process creation-time evidence on Windows and Linux;
- SHA-256 executable, argv, and owner digests without storing raw command line or
  account name;
- honest `create_time_unavailable` on unsupported platforms;
- safe cross-restart adoption only when every owned process verifies;
- `unmanaged_running` for live but unproven processes;
- identity re-verification immediately before every stop signal;
- refusal of partial stop and PID-reuse signalling.

## 5. Unified Connections lifecycle — complete for the supported launcher

New `src/karox/launch_support.py` is the launcher capability layer promised by
the controller contract. It returns stable blocker codes before a child process
is spawned.

`ConnectionController` now owns:

- list/get/status and endpoint/secret resolution;
- wire test;
- launch-support assessment;
- idempotent Start;
- safe Stop;
- safe Restart;
- removal with managed-runtime and credential cleanup rules.

Start/Restart refuse unproven live processes. A running proven runtime returns
`already_running`; degraded/running restart follows stop → verified relaunch.

The MCP Connections TUI and CLI now use the controller. Added CLI operations:

```text
karox connections launch-support ID
karox connections start ID
karox connections restart ID
```

The currently implemented managed launcher is deliberately conservative:
saved ClickUp + Streamable HTTP + bearer + managed tunnel. Unsupported
transport/auth/tunnel combinations return blocker codes instead of pretending to
start.

`tests/test_launch_support.py` pins that capability contract directly: only the
saved ClickUp bearer preset launches; a Quick Tunnel cannot be published as a
stable URL while Tailscale can; a user-owned `custom` origin has no managed
launcher because KaroX does not own its lifecycle; an unresolvable credential
blocks the start while metadata-only assessment trusts the saved reference;
blockers are deduplicated; the payload carries no secret; and
`ConnectionLaunchResult.coerce` never invents success from a mapping, an object,
or an empty endpoint string. 14 passed.

Focused evidence:

- launch-support contract: 14 passed;
- gates + launch support + controller + runtime + identity: 52 passed,
  6 subtests;
- controller/runtime/ClickUp contract: 46 passed;
- controller/TUI/CLI/ClickUp parity: 72 passed;
- latest CLI/web-launcher set: 63 passed, 1 skipped, 15 subtests.

## 6. Unified provider lifecycle — complete first product slice

New `src/karox/provider_controller.py` owns provider, model, selection, and
credential orchestration.

Implemented:

- provider list/details/edit/remove;
- grouped multi-model details;
- credential availability/fingerprint without exposing the secret;
- attach/rotate/clear credential;
- deletion only for an unshared OS-keyring credential;
- environment references are never deleted as keyring accounts;
- model add/remove/alias/select;
- deterministic selected-model repair;
- provider/model live test;
- recoverable provider + credential + model + selection setup transaction.

The interactive provider probe now tests a newly entered API key in an in-memory
credential store. The key is written to the OS keyring only after the live probe
succeeds. If a later model write fails, provider fields, models, selection, and
the previous keyring value are restored.

Provider CLI now uses the controller and adds:

```text
karox provider details PROVIDER
karox provider credential-set PROVIDER
karox provider credential-clear PROVIDER
karox provider remove PROVIDER --delete-credential
karox model repair-selection
```

Provider TUI now supports:

- Enter: secret-free provider details;
- all registered models in one screen;
- direct active-model selection for multi-model providers;
- edit adapter/Base URL/timeout/retries;
- controller-owned test, activation, copy, and safe removal;
- shared credential preservation.

Focused evidence:

- provider backend/CLI/registry/credentials: 62 passed, 69 subtests;
- provider wizard + provider TUI + CLI/controller: 130 passed, 10 subtests;
- transaction rollback regression: 5 passed.

## 6b. Source-independent safety core — new, 2026-08-02 evening

Two services the rest of the frozen scope depends on did not exist in the tree
at all. Both are now implemented and tested, and neither is wired into a
caller yet, so this slice cannot have regressed any existing behavior.

### `src/karox/risk_engine.py` — universal Smart Stop

One classifier for every agent source. The controlling rule is that the origin
of an agent never changes the safety verdict, so there is no separate check for
ChatGPT Web, for an API model and for the TUI.

- four levels: low, medium, high, critical;
- an **unrecognised action kind classifies as high**, so a tool added tomorrow
  cannot bypass Smart Stop merely by not being on a list yet;
- escalation on recursive delete, bulk delete, bulk mutation, large repository
  share, system paths, irreversibility, repository escape, and acting beyond
  what the user asked for;
- push, force push, reset --hard, clean, history rewrite, publish, release,
  deploy, payment, billing, subscription, account deletion and credential
  export are critical and always stop;
- an engine cannot be *constructed* with auto-approval above medium, so the
  stop line cannot be configured away.

Confirmation contract, per section 23.3 of the scope:

- bound to one exact action digest, one session and one target;
- single-use, time-bounded, and dropped once expired;
- tokens stored hashed and compared with `hmac.compare_digest`;
- the token never appears in an assessment, an event or an evidence record, so
  a model cannot read one and a page cannot forge one;
- the digest deliberately excludes `source` and `summary`: rewording must not
  force a second approval, and an identical action from another front end must
  not need a new one.

Evidence: **25 passed, 22 subtests**.

### `src/karox/event_bus.py` — unified event stream

The reason logs became the main UI is that the TUI had nothing else to read.
This is that something else: typed, ordered, bounded and redacted events for
agent actions, tool calls, browser actions, session and connection state, risk
decisions, confirmations, performance spans, health, evidence and errors.

- hard capacity with an honest `dropped` counter instead of a silent gap;
- every payload passes through the existing `karox.security.redact` on the way
  in, so no second redaction implementation was introduced;
- callers may name known secret values that are not secret-shaped;
- a subscriber that raises is counted, not propagated: a broken UI cannot stop
  an agent;
- delivery happens outside the lock, so a subscriber that reacts by publishing
  does not deadlock the publisher;
- `since_seq` gives a UI incremental refresh instead of a full re-read;
- `clear()` empties the view without resetting the sequence and without
  touching repository changes;
- `span()` measures a path and separates KaroX overhead from browser, network,
  provider, subprocess, disk and keyring time, and still records a span when
  the body raises.

Evidence: **19 passed, 2 subtests**.

### Combined evidence for this slice

Release gates, risk engine, event bus, launch support, process identity,
connection runtime and connection controller together: **96 passed,
30 subtests**. Discovery moved to **1073 suite / 1078 root** and all published
copies were updated; `check_test_count` passes.

### `src/karox/risk_mapping.py` — Core commands reach Smart Stop

The adapter between the one place every agent reaches the machine
(`CoreRuntime`) and the RiskEngine. Capability policy answers *may this origin
ever do this*; risk answers *is this specific instance dangerous now*. A profile
can legitimately hold `repo.write`; deleting four hundred files with it is still
not a model's decision.

What the mapping refuses to be fooled by:

- a batch payload that hides thirty edits under one `repo.command` is counted
  as thirty, so it escalates to `bulk_mutation` instead of looking bounded;
- `git push` hiding inside `process.run` maps to `git.push`, and `--force` to
  `git.force_push`; a Windows `git.exe` path is recognised the same way;
- `git reset --hard` is destructive while a soft reset is `git.read`, and
  `git tag -d` is a deletion while `git tag v5` is not;
- `npm publish` and `twine upload` are `package.publish`;
- `browser.command` is scored per sub-action from its payload, so adding an
  action never needs a client reconnect, and an unnamed action is treated as
  input rather than assumed safe;
- an outbound `mcp.*` call is a bounded mutation, never a read;
- an unmapped command name passes through unchanged, so the engine sees an
  unknown kind and returns high.

`CoreCommand` gained `confirmation_token`. It is excluded from `input_digest`
on purpose: the digest identifies the work, and an approval must never change
the identity of the thing that was approved.

Evidence: 23 passed, 15 subtests. Core, policy, agent and session regression
after the model change: 83 passed / 1 skipped / 37 subtests, and 62 passed /
3 subtests. Discovery **1096 suite / 1101 root**, all copies republished,
`check_test_count` green.

### Smart Stop is now enforced in `CoreRuntime`

The gate is in place. `CoreRuntime.__init__` takes optional `risk` and `events`;
`execute` calls `_apply_smart_stop` immediately after the capability checks and
before the mutation lease, so a refused action never reserves an idempotency
intent and never reaches disk.

- no engine passed means no gate, so every existing embedder is unchanged;
- a stop publishes a `RISK_DECISION` event and audits `core.command.stopped`;
- a rejected confirmation audits `core.command.confirmation_rejected` with its
  machine reason, and never the token itself;
- `_publish_risk` swallows its own failures: observability must not be able to
  block an action.

`tests/test_core_smart_stop.py` uses only real Core tool schemas, so a test
cannot pass against a shape the runtime would have rejected anyway: a bulk
`git.commit` over twelve paths stops; a write to a Windows system path is
critical; one human confirmation lets exactly one run through and the replay is
refused; an invented token is refused; the same command from `chatgpt-web`,
`openai-api` and `clickup-mcp` gets the identical verdict.

Evidence: 12 passed, 3 subtests. Bridge and extended core tools: 61 passed,
11 subtests. Discovery **1108 suite / 1113 root**, all copies republished.

### Static gates now pass on this tree — 2026-08-02 late evening

The developer bridge profile was relaunched with the verification allowlist, so
Ruff and Mypy ran against the real tree for the first time in these sessions.

`python -m ruff check src tests scripts`: **All checks passed.** Seven findings
were fixed first, none of them in the safety modules:

- unused `typing.Iterable` in `browser_access.py`;
- unused `typing.Callable` in `provider_controller.py`;
- a dead `target = ...` assignment in `ConnectionController.remove`, replaced by
  the bare resolve plus a comment explaining why the lookup must still happen;
- a module-level `resolve_connection_secret` import in `tui_connections.py` that
  shadowed three deliberate local imports. The local ones were kept: they are
  what lets tests patch the resolver, and an early module-level binding would
  silently defeat that.

`python -m mypy src/karox`: **Success, no issues in 72 source files.** Sixteen
errors were fixed, and two of them were real defects rather than annotation
noise:

- `browser_access.server_url` interpolated a possibly-bytes host straight into
  the URL, which would hand a client `http://b'127.0.0.1':8765`;
- `extension_browser` used `signal.SIGKILL`, which does not exist on Windows,
  in the last-resort Chrome cleanup path: on the primary development platform
  that raised `AttributeError` instead of killing the process;
- `tui_connections` typed the bound `copy_text` callback as unbound, which made
  all six clipboard call sites look wrong and hid whether any of them were;
- narrowing fixes for `values`/`rows` and an explicit `_base_defaults`
  annotation;
- the `push_screen` callback returned the focused widget; it is now a named
  function that returns nothing, per Textual's callback contract.

The two safety-module fixes were `os.sysconf` (absent on Windows, now resolved
dynamically) and a loop variable in `ConnectionRuntimeRecord` validation that
reused a `str` name for an `int | None` value.

Regression after all of it: TUI Connections 10 passed; ClickUp TUI plus
extension browser 27 passed; external browser access plus provider controller
42 passed.

`python -m build --wheel` still fails: the `build` module is not importable in
this interpreter (`No module named build.__main__`). Installing it is a package
operation, so it is left for the operator.

### Two tooling defects found while doing it

**1. A long `tests.run` blocks every write.** `HostedToolRuntime.execute`
acquires an exclusive mutation lease for any mutating tool and heartbeats it
for the whole call. `tests.run` is mutating, so a multi-minute split holds the
lease; every concurrent `repo.write_file`/`repo.edit_file` then raises
`SessionBusy`, which `bridge_error_code` maps to `denied`. A client-side
timeout makes it worse: the request is gone but the server-side call keeps the
lease. This is what looked like "the session lost its write permission" twice
today. Practical rule until it is fixed: never start a full split while edits
are in flight.

**2. Line-ending drift breaks multi-line guarded edits. Root cause found.**
Several existing files are stored with CRLF, while every file written during
these sessions is LF. A guarded edit compares the anchor byte for byte, so a
multi-line `old_string` containing `\n` never matches a CRLF file and comes back
as `invalid_request`, even though the text on screen looks identical. Confirmed
directly: in `tui_connections.py` the anchor
`"...= {}\n            self._base_defaults = None"` was rejected and the same
anchor with `\r\n` applied on the first try.

Practical rule until the tree is normalized: in a CRLF file use single-line
anchors, or write `\r\n` explicitly in a multi-line anchor. Known CRLF files so
far: `src/karox/core.py`, `src/karox/tui_connections.py`.

This is the LF/CRLF drift section 7.2 of the scope asks about, observed live. It
needs a normalization pass plus a `.gitattributes` rule before release, because
it will otherwise produce a diff full of whole-file rewrites the first time any
editor normalizes on save.

### Not yet done for this slice

- the mapping exists and `CoreCommand` now carries a confirmation channel, but
  the gate call inside `CoreRuntime.execute` is **not** in place: the guarded
  surface began rejecting edits to `src/karox/core.py` partway through this
  session, after one trivial edit had already been accepted. The next exact
  step is a single insertion in `CoreRuntime.execute`, immediately after the
  capability checks and before the mutation lease block:
  build the action with `action_for_command`, call `RiskEngine.authorize` with
  `command.confirmation_token`, and publish a `RISK_DECISION` event. One extra
  blank line remains in `core.py` from that accepted trivial edit; it is
  cosmetic and the file is valid;
- the TUI still renders log text rather than the event stream;
- the full deterministic split could not be rerun: a 4-way split exceeds the
  hosted request window, and finer splits were denied by session policy after
  the timeout;
- Ruff and Mypy remain unrun in this session because `checks.run` still carries
  no approved verification allowlist.

## 6c. State at the end of 2026-08-02

Green on this tree: Ruff clean, Mypy clean across 72 files, and the safety and
lifecycle set together at **124 passed / 48 subtests** (release gates, Core
Smart Stop, risk engine, event bus, risk mapping, process identity, connection
runtime, launch support). Published counts are 1108 suite / 1113 root and the
count gate passes.

The complete suite was rerun end to end and is **green**. The 4-way split does
not fit the hosted request window, so the tree was covered as three 8-way parts
plus ten 16-way parts, which together cover every file exactly once:

| Coverage | Parts | Result |
| --- | --- | --- |
| 8-way | 1, 2, 3 | 410 passed, 1 skipped |
| 16-way | 4, 5, 6, 7, 8, 12, 13, 14, 15, 16 | 695 passed, 3 skipped, 1 xfailed |

Why those parts: the splitter assigns file *i* to part `i % n`, so 8-way part
`k` is exactly the union of 16-way parts `k` and `k + 8`. Parts 1-3 ran whole
as 8-way; parts 4-8 were each covered by their two 16-way halves. Aggregate:
**1105 passed, 4 skipped, 1 xfailed**, no failures anywhere.

### Smart Stop is now live for hosted clients — 2026-08-02, after the commits

The gate existed but nothing switched it on: `CoreRuntime` only enforced it
when an embedder passed an engine, and no embedder did. So Smart Stop was
correct, tested, and dead.

`CoreToolBridge` now defaults to the process-wide `risk_engine()` and
`event_bus()`. That is the right place to turn it on first, because the hosted
bridge is exactly how ChatGPT Web, Claude Web, an MCP client and an external
coding agent reach the machine: the most remote agent source gets the same stop
line as a local one. A caller may still inject its own engine for tests.

The bridge deliberately offers **no channel for a confirmation token**. It
builds the `CoreCommand` itself and leaves `confirmation_token` unset, and a
token smuggled into the tool arguments is rejected by schema validation before
risk is even consulted. A hosted agent therefore cannot approve its own action
by any path; approval belongs to the human-facing layer.

`tests/test_hosted_bridge_smart_stop.py`: reads and single writes still pass, a
bulk `git.commit` from a hosted client stops as high risk, the smuggled-token
path is refused, and the decision appears on the event stream. 6 passed.
Regression: hosted bridge plus bridge, 64 passed / 10 subtests.

Note for the next session: the running bridge process will not pick this up
until it is relaunched, because hot reload only watches the workspace worker
modules. After the next `bridge connect`, this control session itself becomes
subject to Smart Stop, which is intended.

### Wheel builds, and building it found a packaging defect

`python -m build --wheel` now succeeds and produces
`karox_runtime-5.0.0.dev0-py3-none-any.whl`. The Chrome extension package data
is inside it, which is scenario 61 of the scope.

The build log also exposed a real defect. The wheel contains
`karox/markdown_render.py`, and that module **does not exist in `src/karox`**.
It is not in the `build_py` copy list either: it was copied from `build/lib`,
which `setuptools` never prunes. So a module deleted from source keeps shipping
in every later wheel, and a clean checkout of the same commit produces a
different artifact than this machine does. Nobody would have noticed from a
green test run, because the tests import from `src`, not from the wheel.

New gate `scripts/check_wheel_contents.py` compares the wheel against the source
tree with the standard library only: it fails on a module in the wheel that is
not in `src/karox`, on a source module missing from the wheel, and on missing
extension package data. Paths are compared in full, so a module moved between
packages is not mistaken for an unchanged one.

`tests/test_wheel_contents.py` pins all of that against synthetic wheels: 9
passed. The release checklist now requires deleting `build/` before the build
and running this gate on the artifact.

Still open, in priority order:

1. delete `build/` and rebuild, then run the new wheel gate on the clean
   artifact; the current `dist/` wheel is known to carry the stale module;
3. coverage, installed-wheel smoke outside the source tree, platform matrices;
4. the TUI still renders log text instead of subscribing to the event bus;
5. line-ending normalization plus a `.gitattributes` rule.

No commit, push, tag or publish has been made in any of these sessions.

## 7. Work still open

Deterministic behavior is green, but release evidence remains open:

1. Restart the durable saved developer profile so the live connector receives
   the final `checks.run` exposure.
2. Run the exact approved static/build commands through that restarted bridge:
   - `python -m ruff check src tests scripts`;
   - `python -m mypy src/karox`;
   - `python -m build --wheel`.
3. Run coverage and installed-wheel smoke outside the source tree.
4. Record Windows/macOS/Linux installation, migration, rollback, and uninstall.
5. Complete live ChatGPT/Claude/provider/Ellipsis conformance records.
6. Complete external beta and final security/release review.

Do not describe KaroX 5 as release-ready until those dated records exist.

## 8a. Bridge profile that grants the full working surface

Nothing new has to be built for this: `bridge saved ensure-developer` already
assembles exactly the profile the work needs. It sets
`DEFAULT_WEB_TOOLS + WRITE_WEB_TOOLS + karox.checks.run`, the Ruff/Mypy/wheel
verification allowlist that `karox.checks.run` refuses to run without,
`AccessProfile.WORKSPACE_WRITE`, the full-suite deadline preset, and a durable
session identity derived from the profile name so a restart keeps the same
Tailscale hostname and the connector does not need reconfiguring.

Run exactly:

```text
karox bridge saved ensure-developer clickup-opus --repository D:\проекты\KaroX-v5 --tunnel tailscale --language ru
karox bridge connect --saved clickup-opus
```

Then reconnect the hosted client: MCP tool discovery belongs to the client
session, so a client connected before the restart keeps the old tool list.

After connecting, `bridge.diagnostics` must show `saved_profile` set,
`write_permission = true`, `repo.write_file`, `repo.edit_file`, `repo.command`,
`tests.run`, `checks.run`, and the three-command verification allowlist. If
`checks.run` is present without that allowlist the launcher is wrong, not the
session: `HostedToolRuntime` refuses that combination by construction.

Do **not** use a Cloudflare Quick Tunnel for this profile. Its hostname expired
twice on 2026-08-02, mid-session, both times.

One operating rule while the lease defect above is open: do not start a full
test split in one client while another is editing. The split holds the
exclusive mutation lease and every write is refused until it finishes.

## 8. Next operator action

When Egor returns, run the idempotent preset and launch it through stable
Tailscale:

```text
karox bridge saved ensure-developer clickup-opus --repository D:\проекты\KaroX-v5
karox bridge connect --saved clickup-opus
```

After that one controlled restart, verify diagnostics show:

- `saved_profile` set;
- `write_permission = true`;
- durable session expiration;
- `repo.write_file`, `repo.edit_file`, `repo.command`;
- `tests.run` and `checks.run`;
- the Ruff, Mypy, and wheel command allowlist.

No commit or push is part of this execution state.
