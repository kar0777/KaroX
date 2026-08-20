# KaroX 5 master execution state

Last updated: 2026-08-20  
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

1. ~~delete `build/` and rebuild, then run the new wheel gate on the clean
   artifact~~ -- **done, 3 Aug 2026**; see the end of section 8;
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

## 9. Typed session view layer (open item 4 of section 6)

Open item 4 was "the TUI still renders log text instead of subscribing to the
event bus". That is now closed for the **status projection only**, and the
per-projection wording matters: a `SESSION_STATE` event proves the status row and
proves nothing about the transcript.

`src/karox/session_view.py` is the single reducer. It folds
`karox.event_bus.Event` values into `SessionSummary` / `SessionDetail` view
models, incrementally through `since_seq`, bounded on timeline entries, tool
calls and tracked sessions, and it never raises on a hostile payload. It emits
**no user-facing prose**: every field is a number, a timestamp or a stable
identifier such as `confirmation_required` or `review_risk`, so the same row
renders in Russian and in English. Payloads are not redacted a second time --
only an explicit per-kind allowlist of keys is copied, so a confirmation token
has no path into a view even if one were published.

`KaroXApp` owns one store per application instance, attached on mount and
detached on unmount, with `SessionStore` used strictly as a one-shot durable
backfill and `EventBus` as the realtime source. Redraws are debounced through
`consume_dirty`, so a burst of two hundred events costs one status update.

Two defects were found and fixed while wiring it:

- lifecycle callbacks were keyed by session id, so a late callback from a
  superseded run of the same session cleared `agent_busy`, dropped the live
  child's process handle and published a terminal status over a run that was
  still working. Runs now carry a monotonic `RunIdentity`, and a callback that
  does not name the current run touches nothing;
- usage attached to a `SESSION_STATE` payload was silently dropped, because the
  store reads usage only in its `AGENT_ACTION` fold. Token usage is published as
  its own typed event, and only when it was actually measured.

`workspace_mode` is deliberately **not** published. No canonical persisted or
configuration field of that name exists in the product, the previous code
hardcoded `repository`, and a guess that renders identically to a fact is worse
than a blank. An unknown `workspace_mode` must not block rendering.

`_poll_agent_history` stays as the compatibility path. The transcript and the
per-tool activity lines have no typed publishers yet, so removing it would freeze
a running agent's screen. `_EVENT_BACKED_PROJECTIONS` names the one projection
that is migrated; add to it only when a real publisher exists.

Checks on this slice: Ruff clean, Mypy clean (73 source files), wheel builds,
`tests/test_event_bus.py` 19 passed, `tests/test_core_smart_stop.py` 12 passed,
`tests/test_tui_clickup.py` 9 passed,
`tests/test_tui_clickup_connection_screen.py` 2 passed,
`tests/test_wheel_contents.py` 9 passed, `tests/test_release_gates.py` 10
passed. Published counts were refreshed from the release gate to 1228 suite /
1233 root; they were never computed by hand.

At the time of this slice the stale-wheel finding of section 6 still reproduced:
the wheel shipped `karox/markdown_render.py` out of an uncleaned `build/lib`.
That is **no longer true** -- see "Stale-wheel blocker: closed (verified
3 Aug 2026)" at the end of section 8, which supersedes this paragraph.

## 8. Session Browser, and the cost of two editors in one file

The Session Browser is now a real screen rather than a formatter:
`SessionBrowserScreen` in `src/karox/tui.py`, opened with `ctrl+o` or `/browser`,
rendering `SessionViewStore` and folding nothing itself.

Final architecture, deliberately narrow:

- **One reducer.** `SessionViewStore` folds the bus; `SessionStore` is read once
  at mount as durable startup backfill and never again as a status source.
- **One renderer.** `_session_row_text(row, english, verbose=True)`. The status
  bar, the `/sessions` live block and the browser share it, so two surfaces
  cannot describe one session differently. `verbose` is a mode of that renderer,
  not a second formatter, and adds identity (`_identity_fields`) and measurement
  (`_measurement_fields`) columns.
- **Deterministic order.** Rows are sorted by session id, not by last activity:
  the store's activity order is right for a status line and wrong for a list a
  cursor moves through, because any session's event would reorder it.
- **Bounded list.** `MAX_ROWS = 100`, with the remainder reported as a count
  rather than silently dropped.
- **Incremental redraw.** The application drains `consume_dirty` in exactly one
  place, `_drain_session_view`, and forwards the same changed set to the status
  bar and to `SessionBrowserScreen.apply_changes`. A second consumer would clear
  the set from under the first, so the screen never calls `consume_dirty`.
- **Selection is a session id**, restored by `_restore_selection`, so an update
  to another row -- or a new row appearing above -- cannot move what Enter acts on.
- **Exactly one primary action** per row, chosen by the store:
  `review_risk` / `stop` / `resume` / `open`. `stop` is refused with an
  explanation unless this process owns the run.
- **No prose in the view model.** Every word comes from the UI catalogs, so a row
  renders in Russian and English from the same identifiers. An unknown
  `workspace_mode` renders as `UNKNOWN_FIELD` (`—`) and is still not invented.
- **No confirmation token can reach a row**: the ledger keeps tokens out of
  events, and the view layer has no key for one.

### Two parallel editors of `tui.py`, and the rule that follows

Two runs edited `src/karox/tui.py` concurrently and each landed a complete
Session Browser. The file ended up with two `SessionBrowserScreen` classes, two
`action_session_browser` methods, two `/browser` routes, duplicate bindings, a
duplicate `ACTION_STOP` import, two sets of row helpers and a dangling call to a
method that only one of them defined. Ruff was red and neither implementation was
wholly in charge.

The resolution was to keep one and delete the other outright, not to merge them:
merging two designs of the same screen produces a third design nobody reviewed.

**Rule: single-file ownership.** One run owns a file for the whole slice. A second
run must not edit a file another run is editing, even in a different region --
guarded edits protect against a stale read, not against two authors with
different plans.

`/browser` is routed but is **not** listed in the slash-command catalogs. That
menu is a curated list of thirteen visible entries whose rendered layout is
pinned by `tests/test_tui_layout.py`; a fourteenth entry pushes `/sponsors` off
the screen. Documenting it there is a deliberate change that needs the snapshot
re-recorded with `KAROX_UPDATE_SNAPSHOTS=1`, which is a human action.
`tests/test_tui_session_browser.py` pins the decision so the entry is not
re-added as a side effect.

Checks on this slice: Ruff clean (`src tests scripts`), Mypy clean (73 source
files), wheel builds. Tests: `tests/test_tui_session_browser.py` 33 passed,
`tests/test_tui_session_view.py` + `tests/test_session_view.py` +
`tests/test_event_bus.py` 124 passed with 49 subtests, `tests/test_tui.py` 106
passed, `tests/test_tui_layout.py` 9 passed with 1 xfail,
`tests/test_tui_selection.py` 10 passed, `tests/test_tui_connections.py` 10
passed, `tests/test_core_smart_stop.py` 12 passed with 3 subtests,
`tests/test_wheel_contents.py` 9 passed, `tests/test_release_gates.py` 10 passed.

Published counts were refreshed from the release gate to 1261 suite / 1266 root
(5 legacy script checks); they were never computed by hand.

## 9. Session Detail screen — 3 Aug 2026

`SessionDetailScreen` in `src/karox/tui.py` is a real screen, opened from the
browser's primary action. It reads `SessionViewStore.detail()` and nothing else:
no `provider_history`, no `session.json`, no subprocess output, no payload dumps.
`SessionViewStore` remains the only reducer and `SessionStore` remains a one-shot
durable startup backfill.

**Browser → Detail.** `open` and `resume` make the session active and open the
detail; `resume` deliberately starts no agent, because resuming the work stays a
task the person types. `review_risk` opens the detail focused on the risk block
and does *not* switch the active session. `stop` keeps the existing stop flow and
does not open the detail. Escape returns to the browser, re-opened with
`initial_selection`, so the cursor is where the user left it.

**One store, one drain.** Both screens are handed the application's store. The
single `consume_dirty` call stays in `_drain_session_view`, which forwards the
changed ids to whichever of the two screens is mounted. The detail screen ignores
a set that does not name its session, so another session's event costs no redraw
and cannot move the scroll. A render fault in either screen is swallowed: the
publisher is the agent.

**Bounds.** Sections are one widget each, not one per row — a two-hundred-widget
timeline is what makes drawing slow and is also what loses scroll position.
Timeline 60 rows, tool calls 20, evidence/errors 10, each reporting what it hid,
plus the reducer's own `truncated_timeline` and `dropped_events` counts.

Honest split of what this slice actually delivers:

- **implemented and contract-tested**: header, timeline, tool calls, errors,
  usage/cost/budgets, git/diff, browser state, evidence, performance spans,
  pending Smart Stop block, incremental update, bounds, RU/EN parity.
- **contract-tested only**: the risk block shows a verdict; there is no approve or
  reject control, because approving must go through the ledger and a button that
  merely looked like it did would be worse than none.
- **legacy, unchanged**: `_poll_agent_history` is still the only source of the
  transcript and the per-tool activity lines. `_EVENT_BACKED_PROJECTIONS` still
  names one projection.
- **not live-tested**: every assertion here comes from the automated suite; no
  manual session was driven through the screen.

Transcript and tool_activity are **not** migrated. Data absent from the screen is
absent because no typed publisher produces it yet, not because the screen drops it.

Checks: Ruff clean (`src tests scripts`), Mypy clean (73 source files), wheel
builds. Tests: `test_tui_session_detail.py` 45 passed, `test_tui_session_browser.py`
33, `test_tui_session_view.py` + `test_session_view.py` + `test_event_bus.py` 124
with 49 subtests, `test_tui.py` 106, `test_tui_layout.py` 9 + 1 xfail,
`test_tui_selection.py` 10, `test_tui_connections.py` 10,
`test_core_smart_stop.py` 12 with 3 subtests, `test_wheel_contents.py` 9,
`test_release_gates.py` 10. Counts refreshed from the release gate to **1306
suite / 1311 root**; never computed by hand.

One defect found while doing it, worth recording because the failure mode is
quiet: `_timeline_entry_text` called a numeric coercion that lives in
`session_view.py`, not in `tui.py`. Ruff caught the undefined name, but the
*behaviour* was that the whole timeline section raised, was swallowed by the
per-section guard and rendered as "no data" — a screen that looked merely empty
while the session had a full history. A per-section guard keeps one bad section
from costing the other eight, and it also hides exactly this, so the fix came with
a test that asserts a measured duration reaches the timeline.

Next slice, in order: a real human Smart Stop decision screen (approve/reject
through the ledger), then Plan/Act, and only then typed transcript/tool_activity
as its own large slice with a process-boundary transport.

## 10. Minimal UX — Slice B complete: A, A2, B1-B6 landed, 4 Aug 2026

The product principle, written down because it is the thing that decides every
later argument: **KaroX is a minimal coding agent, not a dashboard.** The
conversation owns the screen, the agent's work is visible without being a log,
and detail appears on request. Claude Code and OpenCode are the reference for
that *quality* -- few permanent elements, one clear input line, works in an
ordinary un-maximised terminal -- and are deliberately not copied.

### Landed and tested

**One connection entry point.** `/connect` opens the universal hub. The legacy
api/web/both `ConnectionChoiceScreen` is off every production path: it asked the
user to classify a connection before they had seen what already exists, which is
the question the hub answers for them. `_open_connections(focus)` is the single
root flow; `focus` only chooses which section opens.

**Retired names kept working, kept quiet.** `DEPRECATED_COMMAND_ALIASES` maps
`/connections` to the hub, `/providers` and `/models` to AI models, and
`/mcp-clients` and `/bridge` to external clients. No per-use warning: the alias
is not a user mistake, it is a path the product used to offer. `/bridge stop` is
excluded from the alias branch because it is a real action, not a screen.

**A compact command surface.** `VISIBLE_COMMANDS` is eight entries
(`/connect`, `/sessions`, `/workspace`, `/doctor`, `/language`, `/clear`,
`/help`, `/quit`) and `_commands()` filters the shared catalog, so the menu,
`/help` and both languages cannot drift apart. `/verify`, `/ask`, `/sponsors`,
`/mcp` and `/bridge stop` are hidden but **not** removed -- deleting a working
command to shorten a list is a regression dressed up as simplification.

**One adaptive header line.** `_header_line()` replaces five equal status
columns. At 80 columns each column got 16 cells, so `контекст: лимит` filled its
column exactly and collided with the next field, and a model id was cut to
`openai/m` -- which reads as a *different* model to the person checking which one
is selected. The new rule is subtractive: wide shows product · repository ·
model · state, medium drops the product name, narrow keeps product · state, and a
field that does not fit is dropped **whole**, never abbreviated into a plausible
lie. Context occupancy is passed only above `CONTEXT_WARNING_FRACTION`.

Four existing tests were corrected semantically rather than made to pass. The
provider discovery, provider setup and Puter picker tests entered by pressing
digit keys, which in the unified hub opens the MCP clients screen -- they had
stopped exercising the provider wizard at all while still looking like they did.
They now enter through `action_provider_preset()` or the hub's own production
callback. `test_keyboard_both_flow_verifies_api_then_opens_bridge` was replaced
by `test_adding_a_provider_returns_to_the_one_hub`, which asserts the opposite
property: a saved provider does **not** drag the user into the bridge wizard, and
adding a web client is a separate deliberate act from the same hub.

### Step A landed: the shell is now one line of chrome

`_header_line` is wired to production. `compose` yields a single
`#header-status`, and `_refresh_status` gathers the facts and hands them to that
one renderer -- the width rules live in exactly one place, because the copy that
drifts is always the one a screen actually reads.

Removed from the production layout, not hidden: `#repo-status`, `#model-status`,
`#session-status`, `#context-status`, `#bridge-status`, the `#brand` block and
`#sponsor-ticker`. A hidden widget still occupies geometry and still ticks, which
is why they are absent rather than `display: none`. Kept: `#activity`,
`#composer`, `#conversation`.

The sponsor ticker is no longer mounted at all, so it costs no row and cannot
animate behind the work. `/sponsors` still works and still persists the choice --
deleting a working command to tidy the shell would be a regression dressed up as
simplification -- and its confirmation text no longer claims a row is "shown".

Two consequences worth recording:

- **UX-010 is fixed.** `test_a_narrow_window_gives_the_chat_a_usable_share` was an
  expected failure: at 46×14 the brand block, ticker, three-row status bar,
  separators and composer took twelve of fourteen rows however few there were to
  divide, and hiding the ticker bought back exactly one. The cost was structural,
  so removing the structure fixed it. The decorator is gone and the test passes
  with the default settings that used to be the broken case.
- **UX-012 and UX-013 are asserted against the new header.** Field collision and
  silent model truncation were properties of the grid, so those tests now query
  `#header-status`; the retired widgets are not kept alive to satisfy old queries.
- `on_resize` recomputes the header, so a window narrowed after launch stops
  drawing fields that no longer fit.

Context occupancy appears only above `CONTEXT_WARNING_FRACTION` and is the first
field dropped when space runs out. An unmeasured window produces no note at all.

### Honest status after step A

- **implemented and contract-tested**: single `/connect`, hidden aliases, compact
  command surface, `_header_line` breakpoints, the one-line production shell, the
  removal of the five status widgets and the ticker.
- **contract-tested only**: the header is asserted through widget queries and a
  pure-function test; nobody has looked at the running interface.
- **snapshot blocker: closed.** The five snapshots in `tests/test_tui_layout.py`
  that recorded the old three-row chrome were re-recorded deliberately with
  `KAROX_UPDATE_SNAPSHOTS=1` and the diff was reviewed rather than accepted:
  `answer-long-standard`, `chat-ready-narrow`, `chat-ready-standard`,
  `chat-ready-wide`, `command-menu-standard`. The diff shows exactly what step A
  claims -- one `#header-status`, the five status widgets and the sponsor ticker
  absent, the rows they held handed to the conversation, and the composer still
  framed and visible. Neither the snapshots nor the code was bent to match the
  other.
- **not live-tested**: every claim here comes from the automated suite.

Step A is complete. A2 and B1 through B6 follow below; Slice B of the Minimal UX
slice have not been started.

### Step A2 landed: one human-readable activity line

`#activity` is now the single ordinary-mode indicator of what the agent is doing.
It replaces a stack of up to six raw tool lines -- `✓ repo.read_file 2s  ok •
path=src/karox/tui.py` -- with one sentence: *Reading code*, *Updating code · 2
files*, *Running tests · 28s*, *Waiting for confirmation*, *Done · 2 files · 43
tests passed*, *Stopped · step limit reached*, *Check failed · open details*.

Three defects were structural in the old panel, which is why it was replaced
rather than reworded. It named internal functions the reader cannot call, so the
screen taught vocabulary instead of state. It interpolated `result["data"]`
straight into rendered text, so anything a tool returned reached the terminal
verbatim. And it reserved four rows of chrome for plumbing directly above a
conversation that, in a small window, had about eight.

The containment rule is what makes the substitution safe: **nothing free-form
reaches the screen.** A line is assembled from `_ACTIVITY_WORDS`,
`_ACTIVITY_REASON_WORDS` and integers, and no branch in the layer interpolates a
string taken from a tool result. `ActivityAction` is the boundary that enforces
it -- every field is a catalog key or a number, and there is deliberately no
field for a path, a command, a tool name or a message. A hostile payload
therefore has no path to rendered text: not one that is escaped or filtered, but
none at all. Paths are counted, never printed; the count is the fact a glance
wants and the names are in Session Detail, which has the room for them.

Other decisions worth recording:

- **A finished call does not announce itself.** Making one had the line flicker
  "Reading code", "Done", "Reading code" through a fast turn -- motion that
  reports nothing, because the next call is already running by the time the eye
  arrives. The line keeps describing the action until a *different* action
  starts.
- **Idle draws nothing.** A framed "Working" over a finished conversation is a
  claim that something is happening, and whoever believes it waits for an agent
  that stopped minutes ago.
- **One resolver decides the ending.** The summary and the published lifecycle
  event both come from `_run_outcome` through `_ACTIVITY_OUTCOME_KINDS`. The two
  ladders of `report.get` they replace could disagree, and did: a run could be
  drawn green as "completed and verified" while the bus was told it failed on a
  contract mismatch.
- **Unknown is fail-soft, not loud.** A tool absent from `_TOOL_ACTIVITY_KINDS`
  resolves to "Working", and a reason absent from the catalog contributes
  nothing rather than printing `budget_exceeded:output_tokens` at somebody.
- **One row, enforced twice.** `_fit_activity_line` truncates rather than wraps,
  because a wrapped line is two rows taken from the conversation in exactly the
  narrow terminals where rows are scarcest; `#activity` is also capped at
  `max-height: 2` so a line that escaped the helper still cannot eat the chat.
  The second row exists only for a critical failure.
- **Elapsed updates without a full redraw.** A 1 s timer calls `_show_activity`,
  which compares against the text already on screen and returns without touching
  the widget when nothing changed.

`_poll_agent_history` is kept as the compatibility source for the transcript and
for tool_activity, but its tool-activity projection has changed: raw tool history
is now classified and collapsed into one human-readable activity line. It is not
unchanged, and this is **not** a typed transcript/tool_activity migration --
`_EVENT_BACKED_PROJECTIONS` still names one projection and the typed publishers
for those two do not exist.

One stale test was rewritten semantically rather than propped up.
`test_typed_status_does_not_freeze_the_transcript_fallback` ended at
`self.assertIn("call-1", app._steps)`, a private per-call dict the single-activity
contract removed. `_steps` was **not** restored and no shadow state was created:
asserting a container proved the fallback had run but said nothing about what the
user saw, and would have passed just as happily while the screen rendered
`call-1` and `repo.read_file` at them. It now drives the real record shape the
agent writes -- provider `tool_name`, canonical `core_name`, a call id and a
result payload carrying a prompt injection -- and asserts the rendered
`#activity`: exactly one widget, "Reading code" in English and "Читает код" in
Russian, with none of the plumbing or the payload present.

### Honest status after step A2

- **implemented and contract-tested**: the single `#activity` widget replaced in
  place; human action names for reading, searching, editing, testing and
  waiting; changed-file counts; elapsed; the completion summary; human stop
  reasons; fail-soft unknown actions and reasons; RU/EN parity; one row at
  narrow widths; and the structural exclusion of tool names, correlation ids,
  payloads, sequences and internal enums from rendered text.
- **contract-tested only**: every assertion is a widget query or a pure-function
  test. Nobody has watched a real agent drive this line in a real terminal.
- **not live-tested**: no manual run of the interface was performed.
- **not started**: the new `/connect` presentation, Session Browser redesign,
  Session Detail Overview, Smart Stop, Plan/Act, and the typed
  transcript/tool_activity transport. Product slice **B has not been started.**

Checks after A2: Ruff clean (`src tests scripts`), Mypy clean (73 source files),
`python -m build --wheel` succeeded. Tests: the repaired node id passes
individually, `test_tui_session_view.py` 70 passed with 53 subtests,
`test_tui_activity_line.py` 29 passed with 378 subtests,
`test_tui_minimal_shell.py` + `test_tui_layout.py` 23 passed with 14 subtests,
`test_release_gates.py` + `test_wheel_contents.py` 19 passed. `tests/test_tui.py`
106 passed and `tests/test_tui_layout.py` 10 passed were verified by the owner
locally against this diff. Counts refreshed from the release gate to **1362
suite / 1367 root** (legacy script checks: 5); never computed by hand.

Superseded at B1: the full `tests/test_tui.py` completed inside the hosted
transport at a 180 s timeout (106 passed in 47 s). The earlier note that it must
be run by node id no longer holds.

### Step B1 landed: one Connection Hub, one list, one state source

**Architecture.** `/connect` is the only visible connection command and the only
visible root. Ctrl+S now opens the same hub; it used to push the legacy
`ConnectionChoiceScreen` api/web/both wizard, which left one product scenario
with two competing roots -- whichever one a person happened to use decided what
they believed was connected. `ConnectionChoiceScreen` is now reachable in the
source and from **no** production command or key.

The hidden aliases (`/connections`, `/providers`, `/models`, `/mcp-clients`,
`/bridge`) still work, still stay out of the slash menu and `/help`, and open a
section of the same flow through the same controller. `/bridge stop` remains a
real action and is routed before the alias branch.

**What the root screen replaced.** A three-button menu of KaroX's own protocol
families: MCP clients, Model providers, new API provider. Two things were wrong
with it and neither was cosmetic. It asked the user to classify a connection
before showing them what already exists -- the question they opened the screen to
have answered. And the two lists behind it were separate screens over separate
stores, so nothing in the product ever showed everything that is connected in
one place.

The root now answers three questions and no others: what is connected, does it
work, what can I add. A row reads `OpenRouter - claude-opus - works`.

**One state source.** No new connection store was created. `provider_hub_rows`
projects `ProviderRegistry`; `service_hub_rows` projects the existing
`ConnectionController`, which already reconciles a saved target against its live
runtime. `merge_hub_rows` keys on a family-namespaced `row_id` carrying each
store's own identity, so one connection cannot appear twice -- the failure the
two-screen hub made easy. A source that raises costs its own rows and not the
screen: a wedged ClickUp bridge must not hide the provider list.

**Progressive disclosure, enforced by the type.** `HubRow` has exactly five
fields -- `row_id`, `family`, `name`, `detail`, `status` -- and no field for
`adapter_kind`, `transport`, `auth_scheme`, `access_profile`, `credential_ref`,
`port`, `tunnel` or an endpoint. The root cannot show one even by accident, in
the same idiom as the A2 activity line. Those facts are real and one deliberate
step deeper, in the detail and advanced screens that already exist. An absent
fact contributes nothing rather than an em dash: a column of em dashes is a
table admitting it has nothing to say, and this is not a table.

Statuses are four human words (works, stopped, needs attention, error). A
runtime state nobody mapped becomes *error*, not a blank -- something is wrong
that the row cannot name, and silence would read as fine. A provider with a
saved key and no model is *needs attention*, which is precisely the state a
person opens this screen to discover.

**Add entries name products, not protocols**: AI model, ChatGPT / Claude /
ClickUp, Another MCP/OpenAPI. They are addressed by semantic id
(`HUB_ADD_MODEL`, `HUB_ADD_SERVICE`, `HUB_ADD_OTHER`), never by list position,
so reordering the screen cannot silently repoint a flow -- and the tests ask for
them the same way.

**Return path.** A provider saved from the hub returns to the hub, which
re-reads its two sources on mount, so the new row is simply there. Two things
used to follow a save and both were wrong for this journey: focus went to the
composer, so adding two connections meant typing `/connect` twice; and
`_setup_both` could push `BridgeSetupScreen` on top, asking someone who wanted a
model to configure a tunnel. `_setup_both` is only ever armed by the retired
wizard, so no production path reaches that branch. A wizard opened *outside* the
hub still ends in the chat -- the return point is a property of the journey, held
in `_connect_return_to_hub` and cleared in one place.

**Keyboard-first.** One `OptionList` holds the saved rows and the add actions
alike: arrows move through everything, Enter opens what is highlighted, Esc
leaves. Headings are skipped rather than merely disabled, because a cursor that
stops on a heading is a dead key press. Esc from the hub returns to the composer
and does not loop.

**Responsive.** At 46x14 the dialog fits the terminal, the list is intact and
the key hint shortens to one row instead of wrapping. At 120x30 `max-width: 62`
stops the dialog growing a side panel with nothing to put in it: free space is
left free rather than filled with telemetry.

**A test caught a real isolation defect.** The screen tests patched
`session_dir` but not `config_dir`, so the hub read the developer's actual saved
connections -- non-deterministic, and a machine-specific connection name decided
whether an assertion about KaroX's own vocabulary passed. They now run under
`isolated_karox_directories()`.

One stale test was rewritten semantically rather than propped up.
`test_hub_opens_and_routes_to_mcp_clients` pressed the `1` key against the
retired three-button menu. A digit key is a promise about ordering the screen no
longer makes, so the test now asks for the entry by its semantic id and still
pins the routing property it was written for.

### Honest status after step B1

- **implemented and contract-tested**: `/connect` and Ctrl+S opening one hub;
  `ConnectionChoiceScreen` absent from every production path; aliases hidden
  from menu and `/help` and routed to the same controller; the merged list over
  the two existing stores; no duplicate rows; the absence of `adapter_kind`,
  `access_profile`, port, tunnel, transport and credential references from the
  root; semantic add ids; provider save returning to the hub without opening
  `BridgeSetupScreen`; keyboard-only traversal; Esc not looping; 46x14 and
  120x30 behaviour; RU/EN parity; and A1/A2 left intact.
- **contract-tested only**: every assertion is a widget query, a production
  callback or a pure-function test.
- **not live-tested**: no manual run, and **no real connection was made**. The
  provider and service rows are exercised against fakes and an isolated config
  tree; nothing here proves a live API key or a live MCP handshake.
- **not started**: B2 simple provider flow, B3 ChatGPT/Claude/ClickUp flow, B4
  advanced settings, B5 retry/edit/disable/delete, B6 responsive snapshots. The
  per-connection actions (open, re-test, disable, delete with confirmation) are
  still the existing section screens, not yet the hub-row actions B5 describes.
  Session Browser redesign, Session Detail Overview, Smart Stop, Plan/Act and
  the typed transcript/tool_activity transport remain untouched.

Checks after B1: Ruff clean (`src tests scripts`), Mypy clean (73 source files;
one real `getattr` typing defect found and fixed rather than silenced), and
`python -m build --wheel` succeeded. Tests: `test_tui_connection_hub.py` 32
passed with 101 subtests, `test_tui_connect_surface.py` +
`test_tui_connections.py` 24 passed with 32 subtests, `test_tui_clickup.py` +
`test_tui_clickup_connection_screen.py` 11 passed, `test_tui_minimal_shell.py` +
`test_tui_activity_line.py` + `test_tui_layout.py` 52 passed with 392 subtests,
`test_release_gates.py` + `test_wheel_contents.py` 19 passed. The full
`tests/test_tui.py` **did** fit the hosted transport this time: **106 passed in
47 s** at a 180 s timeout. Counts refreshed from the release gate to **1394
suite / 1399 root** (legacy script checks: 5); never computed by hand. Those two
figures are the B1 record and are left as written; the current published counts
are in the B2 entry below.

### Step B2 landed: the standard provider path, and a real leak closed

The standard path is provider, key, model, verify. A known preset already knows
its endpoint and its adapter, so asking for them asks a question whose answer the
product is holding.

Three defects, and none of them was cosmetic.

**Advanced settings were hidden with no way back.** For a known preset the base
URL, the adapter and the connection name were hidden by `preset-technical`, and
nothing revealed them. Hiding a field is progressive disclosure; hiding it
unreachably is a missing feature wearing the same clothes -- a person whose
provider moved to a regional endpoint had to abandon the preset and re-enter
everything as a custom provider. The button beside those fields said "Limits",
which named one of the things behind it and hid the rest.

**The dialog was a fixed 82 columns.** At 46x14 it was wider than the terminal
and the API key input ran off the edge -- the one field the standard path exists
to collect.

**An API key could reach the screen.** `_friendly_probe_error` has a classified
branch that uses `error.safe_message`, and an unclassified fallback that
interpolated `str(error)` raw. The fallback is the branch that runs when a
provider raises something KaroX has no case for -- an SDK error, a proxy error --
and several SDKs echo the Authorization header or the query string in their
message. That was the last hop before a widget.

Implemented:

- a known provider preset hides its technical fields by default;
- Advanced settings brings back the base URL, the adapter and the connection
  name;
- `f2` opens Advanced settings;
- `f3` keeps the existing limits editor reachable, so a working screen did not
  become unreachable when the button it shared was renamed;
- the provider dialog uses `width: 100%` with `max-width: 82`;
- the original `height: 31` / `max-height: 94%` pair is preserved;
- unclassified provider errors pass through the redaction boundary;
- an API key and echoed Authorization data do not reach rendered text.

The height is worth its own note, because changing it was a mistake made and
reverted here. `height: auto` looks tidier and is wrong: `#provider-fields` is
`1fr`, so the dialog grows to fill whatever it is given and the connection form
eats a 42-row window. The existing fixed height with a percentage ceiling
already handled both terminals, and `tests/test_tui.py` was asserting exactly
that -- it caught the regression immediately. Only the width was defective, and
only the width changed.

Contract-tested:

- the known-preset standard flow;
- the Advanced settings toggle, open and closed;
- 46x14 width behaviour;
- key-shaped provider error redaction;
- preservation of the compact height;
- RU/EN parity, keyboard reachability, and the existing A1/A2 contracts.

Not live-tested:

- a real provider with a regional endpoint;
- a real SDK error that echoes the Authorization header;
- a visual check of the form in an actual 46x14 terminal.

One note on how the leak was found, because it nearly hid itself. The test first
appeared to pass: the bridge redacts tool output on its way back, so the
key-shaped fixture came back as the redaction marker and the assertion compared
a marker against a marker. Building the fixture at runtime instead exposed a
genuine leak that had been disguised as a test artefact.

### Honest status after step B2

- **B1 complete.** One Connection Hub, one list, one state source.
- **B2 complete.** The standard provider flow above.
- **B3-B6 not started**: the ChatGPT/Claude/ClickUp flow, the advanced-settings
  slice beyond the provider form, retry/edit/disable/delete on hub rows, and
  responsive snapshots. Product slice B as a whole is **not** finished.
- Session Browser redesign, Session Detail Overview, Smart Stop, Plan/Act and
  the typed transcript/tool_activity transport remain untouched.

Checks after B2: Ruff clean (`src tests scripts`), Mypy clean (73 source files),
`python -m build --wheel` succeeded. Tests: `test_tui_provider_flow.py` 13 passed
with 9 subtests, `test_tui.py::FullScreenAppTests` 55 passed, the five remaining
`tests/test_tui.py` classes 51 passed, `test_tui_connection_hub.py` +
`test_tui_connect_surface.py` + `test_tui_minimal_shell.py` +
`test_tui_activity_line.py` + `test_tui_layout.py` 98 passed with 525 subtests,
`test_tui_connections.py` 10 passed, `test_tui_clickup.py` +
`test_tui_clickup_connection_screen.py` 11 passed, `test_release_gates.py` +
`test_wheel_contents.py` 19 passed. Counts refreshed from the release gate to
**1407 suite / 1412 root** (legacy script checks: 5); never computed by hand.

### Step B3 landed: one standard flow for ChatGPT Web, Claude Web and ClickUp

From the one hub a person picks a service and gets a human scenario: numbered
steps, one address, one status, one check. KaroX picks the parameters.

Implemented:

- one standard service flow, shared by all three services, reached from the hub
  through `HUB_ADD_SERVICE` and from a saved service row;
- ChatGPT Web, Claude Web and ClickUp presentation, each with its own steps
  written from that service's own product surface rather than copied between
  them: ChatGPT wants a custom MCP app, Claude wants a connector, ClickUp wants
  an App Center entry and an explicit "Authorization header" choice;
- defaults read from the production `MCP_CLIENT_PRESETS` catalog. `ServiceView`
  has five fields and no slot for a transport, a tunnel, a port, an auth scheme,
  an access profile, a runtime profile, an endpoint path or a credential
  reference, so the standard screen cannot show one and the UI cannot grow a
  second defaults table that drifts from the launcher;
- a healthy bridge is reused, never disturbed. Opening a flow calls only the
  controller's read paths, so the URL a user has already pasted into a service
  cannot be invalidated by looking at the screen that explains it. Verify checks;
  it never starts or restarts to make a check possible;
- human statuses only: not configured, ready to connect, waiting for the
  service, checking, works, action needed, connection failed;
- retry in place and return to the same hub, which re-reads its two existing
  sources on mount;
- responsive behaviour: `width: 100%` with a `max-width`, and a height that grows
  only as far as the content needs. Nothing in the dialog is `1fr`, which is the
  B2 lesson applied rather than rediscovered.

The honest half of the status model is the part worth defending. Verifying the
bridge proves the bridge. The ChatGPT and Claude presets both record that OAuth
is covered locally and that no live run exists, so a successful check on those
flows resolves to "the bridge works · the external service is not confirmed yet"
and the screen carries a note saying what the user still has to confirm by hand.
Only a conclusive check may say "works", `service_status_is_proven` is the single
place that decides it, and ClickUp's `public_pending` -- loopback answered, public
URL not resolved -- is explicitly not "works", because calling it that sends
somebody to paste an address that is not live yet.

Contract-tested (`tests/test_tui_service_flow.py`, 36 passed with 231 subtests):

- every offered service exists in the production catalog, and the display name
  comes from the preset;
- `ServiceView` has no technical field, and no rendered step or screen names a
  transport, tunnel, port, adapter, access profile, tool or credential;
- each service has numbered steps in both languages, the same count in each, and
  the three scripts differ from one another;
- the hub service entry opens the picker; each service opens its own screen;
- no flow reaches `ConnectionChoiceScreen` and no flow opens
  `BridgeSetupScreen`;
- opening a flow calls neither `stop`, `start` nor `restart`, and an existing
  endpoint is reused and shown;
- verify with no endpoint starts nothing;
- a failure keeps the screen, the steps and the retry, and leaks no secret;
- "works" only after a proven check; a local-only result and a pending public URL
  are both reported as unconfirmed;
- Esc returns exactly one level and does not stop a bridge;
- 46x14 fits and 120x30 grows no side panel;
- keyboard-only navigation;
- a saved service row opens its own screen, and an unrecognised row falls back to
  the generic list rather than guessing a service;
- the A1 header, the A2 activity line, the B1 hub state source and the B2
  provider flow are all still intact.

One test of mine was wrong and was corrected rather than the code. It banned the
word "Authorization" from the whole screen, which fails on required ClickUp
product text: ClickUp's own form calls the working choice "Authorization header",
and the step has to name it. The assertion now bans credentials and wire-level
dumps from the error region instead of banning a word the instructions need.

Not live-tested:

- a real connection to each external service;
- a real OAuth or confirmation round trip;
- a visual check in an actual 46x14 terminal.

### Honest status after step B3

- **B1 complete**, **B2 complete**, **B3 complete**.
- **B4-B6 not started**: the advanced-settings redesign, edit/disable/delete on
  hub rows, and the final polishing and snapshot pass. Product slice B as a whole
  is **not** finished.
- Session Browser redesign, Session Detail Overview, Smart Stop, Plan/Act and the
  typed transcript/tool_activity transport remain untouched. No new bridge
  architecture and no new provider adapter were introduced.

Checks after B3: Ruff clean (`src tests scripts`), Mypy clean (73 source files),
`python -m build --wheel` succeeded. Tests: `test_tui_service_flow.py` 36 passed
with 231 subtests, `test_tui_connection_hub.py` + `test_tui_connect_surface.py` +
`test_tui_provider_flow.py` 59 passed with 142 subtests,
`test_tui_connections.py` 10 passed, `test_tui_clickup.py` +
`test_tui_clickup_connection_screen.py` 11 passed, `test_tui_minimal_shell.py` +
`test_tui_activity_line.py` + `test_tui_layout.py` 52 passed with 392 subtests,
`tests/test_tui.py::FullScreenAppTests` 55 passed and its five remaining classes
51 passed, `test_release_gates.py` + `test_wheel_contents.py` 19 passed. Counts
refreshed from the release gate to **1443 suite / 1448 root** (legacy script
checks: 5); never computed by hand.

### Step B4 landed: one progressive Advanced layer for every connection flow

The standard flows stay simple. Advanced settings open only on a deliberate
action and are one layer with one set of rules, rather than the several old
wizards that each had their own.

Implemented:

- **one Advanced contract**, shared by AI providers and by the ChatGPT / Claude /
  ClickUp service flows: explicit open, Save, Cancel, Validate, values kept
  after an error, Esc back to the standard flow, Cancel applying nothing, Save
  applying once, and a stored secret never rendered back;
- **progressive disclosure**: the standard screen shows nothing technical, and
  `f2` opens Advanced from both the provider form and the service form. `f3`
  stays as a compatibility binding and now opens the same layer, so the B2
  limits editor keeps working but is no longer a separate entry point;
- **provider and service groups differ, because the applicable fields differ**.
  A provider gets connection name, base URL, adapter, key and limits; a service
  gets connection name, transport, MCP path, port and tunnel strategy. Neither
  is offered the other's fields, because a control the flow cannot apply is
  worse than an absent one -- the user believes it did something;
- **permission modes over the existing `AccessProfile`**: Read only, Project
  access, Extended access, one short line each, mapping onto `READ_ONLY`,
  `WORKSPACE_WRITE` and `ELEVATED`. No new permission architecture. The exact
  allowlist is computed from `_PROFILE_CAPABILITIES` -- the policy layer that
  enforces it -- and appears only in the technical detail behind "Show
  permissions", never in a standard flow or the hub;
- **validation** that keeps every typed value, focuses the field at fault,
  reports one short reason and changes nothing in the registry;
- **secret-safe editing**: a stored credential shows "saved" and an empty
  optional box, and an empty box is not a deletion. Revoking a credential is B5
  and is deliberately not reachable here;
- **explicit restart confirmation**: changing transport, port, tunnel or MCP path
  under a live bridge asks first and says the current address will be
  unavailable. Declining leaves the bridge exactly as it was;
- **responsive behaviour**: `width: 100%` with a `max-width`, a bounded scrolling
  body, and nothing `1fr` outside it. The B2 compact-height guarantee is
  preserved rather than rediscovered.

Two decisions worth recording. "Empty means default" is enforced in
`advanced_changed_fields`, which does not report a cleared optional box as a
change -- so clearing a field cannot overwrite a working value with nothing, and
cannot trigger a restart prompt for an edit the user did not make. And
`AdvancedSettingsScreen.applied` is set once and guarded, so "Save applies
exactly once" is a property rather than a hope: a double Enter cannot write
twice.

Contract-tested (`tests/test_tui_advanced_connections.py`, 52 passed with 72
subtests):

- the service standard flow shows nothing technical, and both `f2` and `f3` are
  bound; `f2` opens the Advanced screen;
- Advanced shows base URL and connection name; limits live in the same structure;
- provider defaults equal `provider_preset(...)`, service defaults equal the MCP
  client preset's transport, tunnel and endpoint path; Reset returns to them;
- a service is never offered `base_url`, `adapter`, `context_window` or
  `max_output`, and a provider is never offered `tunnel`, `port`,
  `endpoint_path` or `transport`;
- the three modes are human names in both languages, map onto three distinct
  existing profiles, summarise in one line with no tool names, and their exact
  allowlist matches `_PROFILE_CAPABILITIES`;
- the allowlist appears only in the permission detail screen;
- a saved secret renders as "saved", the replacement box is empty and masked, and
  leaving it empty carries no `api_key` into the applied values;
- validation errors keep the values, apply nothing and stay on screen;
- Cancel applies nothing; Save applies exactly once;
- opening Advanced calls neither `stop`, `start` nor `restart`;
- a cosmetic change saves with no prompt, a transport-level change prompts first,
  declining applies nothing and accepting applies once;
- Esc returns to the standard flow beneath it rather than closing KaroX;
- 46x14 fits within the terminal, 120x30 grows no dashboard;
- Russian renders without falling back to English;
- the A1 header, the A2 activity line and the B1-B3 flows are intact.

Not live-tested:

- a real restart of a running bridge after a transport or port change;
- a real stored-credential round trip;
- a visual check in an actual 46x14 terminal.

### Honest status after step B4

- **B1 complete**, **B2 complete**, **B3 complete**, **B4 complete**.
- **B5-B6 not started**: edit/disable/delete on hub rows, and the final polishing
  and snapshot pass. Product slice B as a whole is **not** finished.
- Session Browser redesign, Session Detail Overview, Smart Stop, Plan/Act and the
  typed transcript/tool_activity transport remain untouched. No new bridge
  architecture, no new provider adapter, and no second connection store.

Checks after B4: Ruff clean (`src tests scripts`), Mypy clean (73 source files),
`python -m build --wheel` succeeded. Tests:
`test_tui_advanced_connections.py` 52 passed with 72 subtests,
`test_tui_service_flow.py` + `test_tui_provider_flow.py` +
`test_tui_connection_hub.py` 81 passed with 341 subtests,
`test_tui_connect_surface.py` + `test_tui_connections.py` +
`test_tui_minimal_shell.py` + `test_tui_activity_line.py` +
`test_tui_layout.py` 76 passed with 424 subtests, `test_tui_clickup.py` +
`test_tui_clickup_connection_screen.py` 11 passed,
`tests/test_tui.py::FullScreenAppTests` 55 passed, `test_release_gates.py` +
`test_wheel_contents.py` 19 passed. Counts refreshed from the release gate to
**1495 suite / 1500 root** (legacy script checks: 5); never computed by hand.

### B5. Managing existing connections: landed, 4 Aug 2026

Open, edit, verify, disable, enable and delete a connection that already
exists -- through the registries that already own it. No second store, no
management dashboard.

**Implemented**

- existing connection detail: Enter on a hub row opens *that record*, not the
  list it belongs to;
- Provider Edit through an explicit `open_provider_editor`, keeping the
  provider ID and creating no duplicate;
- Service Edit through `dataclasses.replace` on the stored target;
- persisted enable/disable on `ProviderRecord` and `McpClientTarget`, with
  strict boolean deserialization;
- real Advanced persistence through an injected apply callback;
- Advanced prefilled from the saved record rather than the preset;
- no ignored editable fields: everything editable is applied, everything
  unapplicable is read-only and says where it changes;
- honest Hub and Detail provider status -- having models is not proof;
- structured Verify through the B3 helpers;
- `update_running_connection` on the controller: exact config rollback,
  best-effort runtime restoration, honest `runtime_restored`;
- disable/delete active-runtime safety with two different warnings;
- responsive management UX at 46x14, 80x24 and 120x30.

**Four production defects found and closed**

1. Editing a *disabled* service silently re-enabled it. The save path built a
   fresh `McpClientTarget` and passed no `enabled`, so the dataclass default
   applied and renaming a parked connection undid the parking.
2. Advanced Save was a no-op. It set `self.applied` and dismissed; nothing
   reached disk while the screen reported success.
3. Advanced promised a restart and performed a bare `registry.put`. The saved
   configuration moved, the live bridge kept serving the old one.
4. The confirmation dialog was a fixed 70 columns and overflowed a 46-column
   terminal -- on the one dialog whose purpose is to be read before something
   irreversible happens.

Also closed: Advanced opened ClickUp's panel for every service; a provider ID
looked like an editable rename control; the Advanced API-key box was editable
and ignored; `_edit_provider_id` was written and never read.

**Contract-tested**

`test_tui_connection_management.py` 81 passed with 54 subtests,
`test_connection_enablement.py` 35 passed with 35 subtests,
`test_tui_advanced_connections.py` + `test_tui_connections.py` 62 passed with
72 subtests, `test_tui_service_flow.py` + `test_tui_provider_flow.py` +
`test_tui_connection_hub.py` + `test_tui_connect_surface.py` 96 passed with
375 subtests, `test_registry.py` + `test_provider_controller.py` +
`test_connections.py` + `test_connection_controller.py` + ClickUp screens
87 passed with 48 subtests, `test_tui_minimal_shell.py` +
`test_tui_activity_line.py` + `test_tui_layout.py` 52 passed with 392
subtests, `tests/test_tui.py` 106 passed, `test_release_gates.py` +
`test_wheel_contents.py` 19 passed. Ruff clean; Mypy clean over 73 source
files; wheel built. Counts refreshed from the release gate to **1612 suite /
1617 root** (legacy script checks: 5); never computed by hand.

The service-secret contract is pinned at the *mounted* form, reached the way a
person reaches it -- list, select, Edit -- rather than by inspecting source:
an existing record has no `mcf-secret` input at all, shows the note instead of
the add-flow placeholder, and keeps its `credential_ref` and fingerprint
across Save, Cancel and a rejected Save, each verified by re-reading the
registry. The add flow keeps its secret field, so the two are not confused.

**Not live-tested**

- a real external ChatGPT/Claude/ClickUp round trip;
- a physical tunnel restart against a real public URL -- the restart tests use
  a fake runtime;
- guaranteed runtime restoration after every possible launcher failure;
  `restart` stops the old process before launching, so recovery is
  best-effort by construction and the result says so;
- service credential rotation, which remains **unsupported** in the generic
  edit form: a managed bridge validates its bearer against the live keyring,
  so rotation needs a restart-and-handshake transaction this architecture does
  not have. The form says so instead of offering a control that always fails;
- visual inspection in a physical 46x14 terminal.

### B6. Connection polish: landed, 4 Aug 2026 -- Slice B complete

No new features. B6 exists to make the six slices agree with each other, which
is the one property no single-flow test can see.

**The inconsistency found and fixed**

The saved-connection lists called verification **Test** in English while the
detail screen and the service flow called it **Verify**. One action with two
names reads as two actions, and "test" additionally suggests a dry run that
changes nothing -- while this one records a result the screen then displays.
Russian was already consistent at "Проверить"; only the English drifted.

The rest of the audit found the contracts already held, and the new suite now
pins them rather than leaving them true by accident: Hub and Detail share one
status vocabulary, "works" is reachable only from a proven check, disabled
outranks every other status, both families offer the same five actions in the
same order with Delete always second-to-last, a service is never offered
provider-only fields, and `/connect` is the only connection command in the
menu while the five retired aliases still work unlisted.

**Contract-tested**

`test_tui_connect_polish.py` 29 passed with 48 subtests -- vocabulary
consistency, status honesty, detail shape, command surface, keyboard contract,
RU/EN confirmation parity, hub responsiveness at 46x14 / 80x24 / 120x30, cursor
placement on an empty hub and after a vanished row, and the guard that stops a
second verify racing the first.
Regression: `test_tui_connection_management.py` +
`test_tui_advanced_connections.py` 133 passed with 126 subtests;
`test_tui_provider_flow.py` + `test_tui_service_flow.py` +
`test_tui_connection_hub.py` + `test_tui_connect_surface.py` 96 passed with
375 subtests; `test_tui_connections.py` + the four store suites +
`test_tui_clickup_connection_screen.py` 88 passed with 48 subtests;
`test_tui_clickup.py` 9 passed; `test_tui_minimal_shell.py` +
`test_tui_activity_line.py` + `test_tui_layout.py` 52 passed with 392
subtests; `tests/test_tui.py` 106 passed. Ruff clean; Mypy clean over 73
source files; wheel built. Counts at the close of Slice B were **1641 suite /
1646 root** (legacy script checks: 5), taken from the release gate and never
computed by hand. C has since raised them; see the C record below.

**Deliberately not done**

No dead-code sweep: the audit found no *proven* dead surface after B5, and the
hidden aliases, `/connections`, the F3 advanced path and the legacy presets are
all compatibility that B6 explicitly preserves. Removing something merely
unused-looking is how a polish slice becomes a regression.

No snapshot updates: B6 changed one English button label on two screens that
no snapshot covers, so accepting a snapshot diff would have meant accepting
noise.

**Not live-tested**

- real external ChatGPT/Claude/ClickUp confirmation;
- real provider credentials for every preset;
- physical tunnel restart under every launcher failure;
- service credential rotation, because it is unsupported;
- visual inspection in a physical 46x14 terminal.

**Slice B is complete.** B1 through B6 all landed.

### Live acceptance — ConnectionDetailScreen mount crash fixed, 4 Aug 2026

Live acceptance (section 14 of the live-acceptance brief) found a hard crash
that the Slice B suite never surfaced: opening `ConnectionDetailScreen` for a
**provider**, a **stopped** service, or a **disabled** service raised
`SignalError: Node must be running to subscribe to a signal (has
ConnectionDetailScreen() been mounted?)` during mount.

**Root cause: shadowing Textual `MessagePump._running`.** The screen stored its
own "is the bridge running" flag on an attribute named `self._running`. That
name is owned by Textual's `MessagePump` and means *this node's event loop is
alive* (it backs `screen.is_running`). A provider has no process and sets its
bridge flag `False`; a stopped or disabled service sets it `False`. Each of
those writes flipped `screen.is_running` to `False` mid-mount, and Textual then
refused to let the now-"stopped" node subscribe to signals -- so mount raised.

**Why a running bridge masked the defect.** A running service set the bridge
flag `True`, which happened to agree with the framework's `_running`, so that
one path mounted and the screen looked fine. The defect only fired for the
non-running shapes -- exactly the ones a person opens to *check*, *disable* or
*delete* a connection.

**Why the Slice B tests missed it.** Every existing connection-flow test
intercepted `push_screen` and asserted on the *constructed* screen object, not
the *mounted* one. The crash happens between push and the first render, so it
never ran under those tests. The whole point of the new suite is to mount for
real.

**Production fix** (`src/karox/tui_connections.py`, `ConnectionDetailScreen`):
renamed the connection flag `self._running` -> `self._bridge_running` at all
seven sites (init, provider read, service read, `view()`, the advanced-screen
`bridge_running=` argument, and the disable/delete warnings). The `view()`
dict key stays `"running"` -- only the value source changed. The framework's
`_running` is never touched. No `delattr`, no `SignalError` swallowing, no
`call_later`, no sleep, no global monkey patch, no Textual change.

**Other collisions scanned.** Both TUI modules (`tui.py`, `tui_connections.py`)
were scanned for assignments to the Textual-reserved names (`_running`, `_name`,
`_parent`, `_mounted`, `_closing`, `_closed`, `_disabled`, `_id`, `_classes`,
`_screen`, `_app`, `_message_queue`) on every `Screen`/`ModalScreen`/`Widget`
class. The only collision was `_running`; the earlier `_name` -> `_display_name`
fix is intact and was not reverted.

**Tests added** (`tests/test_tui_connection_detail_lifecycle.py`, 23 tests /
38 subtests): a real mounted pilot suite covering provider, stopped
ChatGPT/Claude/ClickUp/Custom-MCP, disabled service, and running service; for
each it mounts the screen through the production read path and asserts
`isinstance(app.screen, ConnectionDetailScreen)`, `screen.is_running is True`,
the right identity and human-readable status, and `view()["running"]` honestly
describing the bridge (not the screen). It also covers the real Hub -> Enter ->
Detail journey, Esc dismissal, three consecutive opens (one Detail instance per
round), a rapid double-Enter (no stacked detail screens), a stale record
removed between render and Enter, RU + EN + a live language switch while a
detail is open, a provider with no endpoint, and a running service showing its
endpoint.

**Recurrence protection (two levels).** Level 1 is behavioural: a mounted
detail with `_bridge_running=False` keeps `screen.is_running is True`, and
toggling the bridge flag during `refresh_view` never changes the framework
lifecycle. Level 2 is a static AST contract (`FrameworkAttributeContractTests`)
that parses `tui_connections.py` and `tui.py` and asserts no
`Screen`/`ModalScreen`/`Widget` class assigns the reserved framework attributes
-- specifically `self._running` and `self._name` on `ConnectionDetailScreen`.
It walks the AST rather than substring-grepping, so a renamed helper or a
doctored `inspect.getsource` cannot hide a re-offend.

**Regression.** Focused: the new lifecycle suite plus `test_tui_connection_hub`,
`test_tui_connect_surface`, `test_tui_connection_management`, `test_tui_service_flow`,
`test_tui_provider_flow`, `test_tui_connect_polish`, `test_tui_advanced_connections`,
`test_connection_enablement` -> **316 passed, 584 subtests**. Broader:
`test_tui` + the nine session/layout/activity suites -> **431 passed, 665
subtests**. Ruff: clean over `src tests scripts`. Mypy: clean over 73 source
files. Wheel built: `dist/karox_runtime-5.0.0.dev0-py3-none-any.whl`.

**Release gates.** First gate run surfaced new authoritative counts
(**1754 suite / 1759 root**, was 1731 / 1736) because the new lifecycle suite
added tests. All seven published copies were updated (README.md, README_RU.md,
docs/IMPLEMENTATION_STATUS.md x2, docs/RELEASE_CHECKLIST.md, docs/vNext/README.md
x2). Gate re-run: 10 passed. Wheel-contents re-run (the required second fully
green run): 9 passed.

**Wheel install.** The global `karox` was an editable install pointed at `src`.
The freshly built wheel was installed (force-reinstall, `--no-deps` so no
credential store, keyring, or other package was touched) into the Python that
owns the global `karox.exe` (Python 3.13.11). After install, `import karox`
resolves to `site-packages\karox\` (not `src/`), `karox.tui_connections.__file__`
is under `site-packages`, and the fix (`self._bridge_running`, no `self._running`)
is present in the installed file. `karox bridge saved show clickup-opus`
survived the reinstall with its credential reference intact.

**Clean-wheel smoke.** A throwaway venv installed only the wheel and its runtime
deps; `import karox` resolved into the venv's site-packages; a headless journey
(App -> /connect -> stopped saved row -> Detail -> Esc -> Hub) mounted and kept
`is_running is True` for provider, stopped ClickUp and stopped ChatGPT, three
times each (9/9). No real credential was used: an isolated config tree held a
fake registry/credential reference.

**Global install smoke.** From a different folder
(`D:\проекты\test-glm52-fable-site`) the same journey via the global
wheel-installed `karox` mounted all nine cases with `is_running is True`. The
live Hub -> Detail path no longer crashes.

**Browser-enabled ClickUp bridge.** The existing `clickup-opus` saved profile
was edited to `tunnel: cloudflare`, `access_profile: workspace_write` (identity
unchanged, so the existing OS-keyring token is preserved -- never printed, never
re-created). Browser capability is additive at connect time
(`profile.X or cli_flag` in `_saved_profile_connect_config`), confirmed via
`--diagnostics-only`: `external_https`, `headed`, `user_takeover`,
`network_inspection` all effective `true`; `payment_confirmation` stays `false`.
`karox browser.request_user_takeover` / `resume_after_user_takeover` surface only
when takeover is enabled (they are connect-time tools, not profile-persisted).
Note: `cloudflared` is **not installed** on this machine, so a cloudflare tunnel
cannot actually launch here; `tailscale` is installed and ready and is an
accepted ClickUp tunnel (`MANAGED_TUNNELS`). The existing ClickUp bridge was
already running under `tailscale` (`https://monsterpc.taila81286.ts.net/mcp`,
stable device-hostname URL) with the old credential: `state: running`,
`bridge_alive: True`, `tunnel_alive: True`, `identity_verified: True`, and both
local (`127.0.0.1:8765`) and public endpoints return `401` to an unauthenticated
probe (correct: the bridge is up and enforces auth).

**Authoritative counts after this change:** **1754 suite / 1759 root**
(legacy script checks: 5), taken from the release gate. Both gate runs green.

**Not live-tested** (still contract/programmatic only):

- a headed Chromium window opening a public HTTPS page through the bridge --
  Chromium is installed (`ms-playwright/chromium-1161`) and the effective browser
  flags are verified, but driving a headed session with user takeover needs a
  live agent run against the bridge, which is interactive and long-lived and was
  not executed from this session;
- manual user-takeover confirmation in a live headed session;
- connecting ClickUp itself to the MCP endpoint (the external connector side),
  though the bridge is up and the URL is reachable with `401`.

**Status after this fix:** UI roadmap implemented. Live connection-detail path
accepted. ClickUp bridge accepted with existing credential. Workspace write,
tests, checks and headed external browser verified at the configuration level;
the interactive headed-browser + takeover run remains not live-tested.



**Status:** A1-A2 complete; B1-B6 complete; Slice B complete; **C complete**;
D not started.

The browser answers one question -- which session to open -- and C is mostly a
record of things taken *out* of it. It was a dashboard: full session id, access
profile, workspace mode, raw `current_step`, event summary, error count,
tokens, cost, budgets and a per-row action, all in a list a person scans to
pick a row. Every one of those facts still exists in Session Detail, which is
where somebody who has already chosen goes looking.

**Implemented**

A separate compact presentation contract (`_session_browser_row_text`) beside
the technical one, not a third mode inside it: `/sessions` is a record of what
happened and the browser is a chooser, and one renderer asked to be both serves
neither. What they share is the *source* -- one `SessionSummary`, one set of
status and action catalogs -- so the words cannot disagree between surfaces.

Task-first rows; human status; human activity mapped through the existing A2
classification (`_canonical_tool_name` + `_TOOL_ACTIVITY_KINDS`), with an
uncatalogued tool rendering nothing rather than the generic "Working" that
`_activity_kind_for_tool` would give; raw tool identifiers never shown. Width
measured from the actual content region rather than `app.size.width`, with
separate row and footer budgets because the footer is a child of the dialog and
the rows are not. Whole-field dropping in a fixed order, and an elastic task
that absorbs what is left. Whole short action spellings for the narrow footer.
A width-aware empty state that degrades to a shorter *complete* sentence.
Readable `max-width` on the dialog. Responsive at 46x14 / 80x24 / 120x30.
Incremental redraw of both rows and footer. Stable selection held by session id
with neighbour hand-over on removal. Localized title, rows, footer and empty
state through one refresh method. Safe one-line task normalization. Honest
overflow. `/sessions` output unchanged.

**Contract-tested**

`test_tui_session_browser.py` + `test_tui_session_browser_compact.py` **92
passed with 109 subtests**. `test_session_view.py` 35 passed;
`test_tui_session_view.py` 70 passed with 53 subtests;
`test_tui_session_detail.py` 45 passed with 48 subtests.
`test_tui_minimal_shell.py` + `test_tui_activity_line.py` +
`test_tui_layout.py` 52 passed with 392 subtests. Slice B regression:
`test_tui_connect_polish.py` + `test_tui_connection_hub.py` 61 passed with 151
subtests; `test_tui_service_flow.py` + `test_tui_advanced_connections.py` 89
passed with 303 subtests; `test_tui_connection_management.py` 81 passed with 54
subtests. `tests/test_tui.py` 106 passed. Ruff clean; Mypy clean over 73 source
files; wheel built; `test_release_gates.py` + `test_wheel_contents.py` 19
passed on the second run. Counts at the close of C were **1700 suite / 1705
root** (legacy script checks: 5), taken from the release gate. D has since
raised them; see the D record below.

**Defects found and closed in C**

Five, four of them self-inflicted and caught by the tests rather than by
reading. The activity field looked `current_step` up in `_EVENT_SUMMARY_TEXT`,
which holds lifecycle summaries -- it never crashed and never leaked, it simply
missed on every real step, so the column silently never worked. `_width()`
returned the terminal width, which is not the row width, so the renderer
spent columns that do not exist and Textual wrapped the line. The id fallback
sliced a tail and rendered `s-summary` as "Session ummary". A fixed task budget
fitted in English and pushed a long Russian row past 80 into a wrap. The footer
repainted on every dirty update, including another row's, undoing half the
point of the incremental redraw.

One rejected shortcut worth recording: the last-resort footer branch
character-truncated the action, which would print `Enter: \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u2026` and ask
the reader to guess what the key is about to do -- exactly the guess a
confirmation exists to prevent. It was replaced with whole shorter spellings,
and `review_risk` shortens to "review" rather than "confirm" because Enter
opens the decision and does not approve it.

**Not live-tested**

- visual inspection in a physical 46x14 terminal;
- hundreds of real long-running sessions;
- multi-process stop ownership beyond the existing contract tests.

## D: the overview-first Session Detail (complete, 4 Aug 2026)

**Status:** A1-A2 complete; B1-B6 complete; Slice B complete; C complete;
**D complete**. The current UI roadmap is finished.

The screen used to open on `_session_row_text(verbose=True)`: an internal
session id, an access profile and a raw tool name, above eleven permanently
mounted sections most of which said `no data`. A person arriving to find out
whether their task worked read telemetry first and prose never.

**Implemented**

A separate pure presentation contract over the same `SessionDetail`, for the
same reason C gave the browser one: `_detail_overview_lines`,
`_detail_attention_lines`, `_detail_progress_lines` and `_detail_check_outcome`
answer "what is this, does it need me, did it work", while the technical
renderer keeps answering "what exactly happened". Nothing was deleted -- the
identity line moved to a `diagnostics` section at the bottom, where somebody
debugging will look for it.

Sections reordered to attention, progress, errors, timeline, tools, usage,
workspace, browser, evidence, performance, diagnostics. Optional sections are
hidden when empty rather than unmounted, so the DOM stays fixed and a section
appearing costs a visibility flag instead of a rebuild. Attention is absent
when nothing is waiting: the permanent "No confirmation is pending" card
trained readers to skip the one region that would some day matter.

Verification is three-valued and defaults to "not confirmed". Only a finished
tool call in the testing family carrying an explicit `ok` counts; a diff, a
completed run, a command whose name contains "test" and a call that merely
started are all rejected as proof.

One-line responsive footer with whole-hint dropping, `max-width: 100`, widths
measured from the real content region with separate body and footer budgets,
localized overview/attention/titles/bodies/footer through one refresh method,
and a bounded header pinned above the scroll so the overview cannot fall below
the fold.

**Defects found and closed in D**

Two, both real and both invisible to a reading of the diff. `focus_risk` still
called `scroll_to_section("risk")` after that section was replaced by
`attention`, so `review_risk` -- the one route whose entire purpose is to put a
person in front of a decision -- scrolled them to nothing. And the application's
language switch only iterated over `SessionBrowserScreen`, so an open Session
Detail kept an entire English document above a Russian shell despite having had
a working `refresh_language` all along.

The confirmation scroll is now keyed on the decision's identity rather than its
presence: it fires on arrival and when a *different* action is proposed, and
not on the tick in between, because a screen that jumps every second is one
nobody can read the timeline of before deciding.

**Contract-tested**

`test_tui_session_detail.py` **45 passed with 84 subtests**;
`test_tui_session_detail_overview.py` **31 passed with 27 subtests**.
`test_session_view.py` 35 passed; `test_tui_session_view.py` 70 passed with 53
subtests. Browser: `test_tui_session_browser.py` +
`test_tui_session_browser_compact.py` 92 passed with 109 subtests.
`test_tui_minimal_shell.py` + `test_tui_activity_line.py` +
`test_tui_layout.py` 52 passed with 392 subtests. Slice B:
`test_tui_connect_polish.py` + `test_tui_connection_hub.py` 61 passed with 151
subtests; `test_tui_service_flow.py` + `test_tui_advanced_connections.py` +
`test_tui_connection_management.py` 170 passed with 357 subtests.
`tests/test_tui.py` 106 passed. Ruff clean; Mypy clean over 73 source files;
wheel built; `test_release_gates.py` + `test_wheel_contents.py` 19 passed on
the second run. Counts refreshed from the release gate to **1731 suite / 1736
root** (legacy script checks: 5); never computed by hand.

**Known cosmetic defect, deliberately left**

A session with no task and a hyphenated non-minted id renders its fallback from
the last hyphen-separated segment, so `s-broken-section` reads "Session
section". The rule is right for the minted `task-<epoch>-<suffix>` ids the
product actually creates and wrong for hand-written ones, which appear in tests
and not in the field. Changing it means changing a C contract that three width
budgets are pinned against, and doing that at the end of D is how the earlier
"Session ummary" defect happened. Recorded rather than rushed.

**Not live-tested**

- visual inspection in a physical 46x14 terminal;
- hundreds of real long-running sessions;
- real external provider errors in every format;
- multi-process stop ownership beyond the existing contract tests.

### Stale-wheel blocker: closed (verified 3 Aug 2026)

`build/` was cleared by hand, and the blocker of sections 6 and 7 is **resolved**.
Rebuilt with `python -m build --wheel`; the log shows `creating build\lib\karox`,
so the tree was regenerated from `src/` rather than reused.

- wheel: `dist/karox_runtime-5.0.0.dev0-py3-none-any.whl`
- `karox/markdown_render.py`: **absent**
- module sets: **identical** -- 73 modules in `src/karox` (72 top-level plus
  `browser_extension/__init__.py`), 73 in the wheel, no extras either way
- `karox/session_view.py`: **present**
- extension package data: all eight files present, including
  `browser_extension/manifest.json` and `service_worker.js`
- `tests/test_wheel_contents.py`: 9 passed

One honest limitation. `tests/test_wheel_contents.py` exercises the gate logic
against synthetic wheels, which is what keeps it fast and deterministic; the gate
run against the *real* artefact is `python scripts/check_wheel_contents.py`, and
that is outside the three commands `checks.run` permits. The real wheel was
therefore verified from the build's own `adding ...` RECORD listing compared
against `src/karox` -- direct evidence about this artefact, not a synthetic
stand-in. A human should still run the script before publishing; the release
checklist already requires it.

## 11. Multiproject Pass 2 closure — 2026-08-20

### Bypass architecture (verified)

- `src/karox/access_mode.py` is the single access-mode contract:
  `build_saved_profile_bypass`, `saved_profile_bypass_enabled`,
  `provider_bypass_enabled`, `set_provider_bypass`, `provider_access_profile`.
- `ProviderRecord.bypass` defaults to **False** everywhere; records written
  before the field existed read back as OFF.
- Bypass ON yields `AccessProfile.ELEVATED` for the runtime/session; OFF keeps
  the normal policy. Persisted per connection/provider, idempotent, reversible;
  identity, endpoint, port, public URL, and credential refs survive the flip.
- No secret material is written to the profile store.
- Hyperagent "full access" is a compatibility wrapper over the shared contract:
  legacy full-access records read as bypass ON.

### Provider wiring (verified, data-driven)

One contract matrix (`tests/test_bypass_matrix.py`) instead of per-product
special cases:

- saved-profile families: ChatGPT, Claude, Hyperagent, Adapt (alias of the
  chatgpt-web bridge), Notion;
- record-backed families: ClickUp, PromptQL, web-agent, IDE, custom,
  generic MCP — the mode lives on the connection record;
- providers: builtin and custom API behave identically; provider API key,
  base URL, and model config are untouched by bypass.

### Adapt shared/native flow (verified)

- Shared: Adapt reuses a compatible running ChatGPT bridge (`chatgpt-dev`)
  instead of launching a second process; no second secret, URL stays stable,
  credential does not rotate. `owns_saved_bridge("adapt")` is False, so
  deleting the Adapt binding cannot delete the shared bridge. Record-backed
  families never share a physical bridge, so widening one cannot widen another.
- Native first run: `target_profile=adapt`, local port 8769, Tailscale Funnel
  HTTPS 10001, `/mcp` bearer endpoint; browser and dev-server capabilities
  default OFF and require explicit opt-in. Root `/connect` shows the real
  Adapt state without a fake duplicate runtime.

### Stale project registry defect — historical-only

`durable_supervisor.last_error` ("saved project registry is invalid: project
path does not exist ...Temp...test_project") predates the successful restart;
the bridge has loaded the strict profile validation cleanly since
(restart_count=1, healthy heartbeats). The current on-disk registry is valid.
No code change required; `last_error` is a sticky historical field.

### Exact tests (all green, 2026-08-20)

- `tests/test_access_mode.py`, `tests/test_bypass_matrix.py`,
  `tests/test_bypass_wiring.py`, `tests/test_tui_advanced_connections.py`:
  **91 passed, 134 subtests**.
- `tests/test_tui_service_flow.py`, `tests/test_core_smart_stop.py`,
  `tests/test_hosted_bridge_smart_stop.py`, `tests/test_multi_project_bridge.py`,
  `tests/test_project_context.py`, `tests/test_project_registry.py`,
  `tests/test_repository_lease.py`, `tests/test_workspace_change_guard.py`,
  `tests/test_workspace_picker_v1.py`, `tests/test_affected_checks_nested_lease.py`,
  `tests/test_ellipsis_lease.py`: **124 passed, 1 skipped, 434 subtests**
  (durable job job-40f0d1f1957e0c46be48).
- Ruff: **GREEN** (`All checks passed!`).
- Mypy: **GREEN** (exit 0, `python -m mypy src/karox`).
- Full pytest (one durable job `job-c1c20253f90a4b946e8c`): **2820 passed,
  5 skipped, 1847 subtests passed in 859.93s (0:14:19), exit 0**.

### Build

- Wheel build: **GREEN** (`python -m build --wheel`, exit 0, durable job
  `job-ef71e6f19617762d4574`).
- Wheel contents gate + release gates + installed-wheel smoke
  (`tests/test_wheel_contents.py`, `tests/test_release_gates.py`,
  `tests/actual_wheel_contents_acceptance.py`): **21 passed, exit 0**.

### Live verified vs not live verified

Live verified (through the running chatgpt-dev bridge):

- bridge health, strict profile load, durable supervisor recovery;
- hosted tools runtime end-to-end (this session ran entirely through it);
- registry validity after the historical stale-path incident.

Not live verified (covered by automated Textual/unit suites only):

- interactive Ctrl+W WorkspaceManagerScreen add/select/remove/default on the
  host TUI, path-with-spaces entry by hand, and the human-driven shared-bridge
  stop confirmation dialog. These have deterministic UI tests but no manual
  pass in this session.

### Remaining

None for multiproject-pass2. The only open item outside this workstream is
the Pass-1 controlled live migration/restart of saved profile `chatgpt-dev`
(deliberately deferred; the working bridge must not be restarted casually).

## 12. Release-live Pass 3 — production/test isolation enforced, 2026-08-20

### Production/test isolation is now architecture, not convention

The `rehearsal-test` contamination class is closed end to end:

- `tests/_path_setup.py` (imported by every test module, pytest and unittest
  alike) captures the machine's real config/runtime/legacy locations into
  `KAROX_TEST_FORBIDDEN_DIRS` *before* anything is patched, then redirects all
  five override spellings to a fresh per-process sandbox and arms
  `KAROX_TEST_ISOLATION=1` plus `KAROX_TEST_SANDBOX_DIR`.
- `karox.paths` guards every resolver: resolution landing inside a captured
  real location is re-pointed at the process sandbox; a process without a
  sandbox (an unprepared child) fails fast with `TestIsolationViolation`.
  Re-basing `APPDATA`/`XDG_*` at a temporary directory remains a legitimate
  isolation style and is untouched.
- `karox.credentials` refuses the real OS keyring while isolation is active;
  suites that deliberately exercise the production keyring path opt in
  visibly with `KAROX_TEST_ALLOW_REAL_KEYRING=1` (clickup setup/TUI; replacing
  that with an injected fake keyring backend is recorded as post-v5 work).
- Regression: `tests/test_production_isolation_guard.py`, subprocess-based so
  suite-level environment mutations cannot fake or break the verdict.

The guard exposed real latent defects, all fixed: alias-only overrides
(`KAROX_RUNTIME_DIR` without `KAROX_VNEXT_RUNTIME_DIR`) in
`test_web_bridge_launcher`, `test_bridge` (4 sites), `test_clickup_setup`
(4 sites); `_tui_harness.isolated_karox_directories` popped the legacy
override and let children read the real `RepoPilotBridge` directory;
`test_bridge`/`test_handoff` `_cli` child environments missed the
`KAROX_VNEXT_*`/legacy spellings; `test_default_dispatch` asserted provider
state that only existed on the developer's machine (now asserts the command
contract plus the built-in preset catalog); `test_bootstrap_portable` now
probes that `bash` actually works before using it, because `shutil.which`
finds the WSL launcher stub on Windows hosts.

### Repository hygiene

- `.gitattributes` added: deterministic line endings (LF for source/docs,
  CRLF for PowerShell/batch, binary rules), no tree-wide renormalize.
- `.gitignore` extended so machine-local junk observed in this working copy
  (`miraism/`, `NUL`, outreach research notes, `.commandcode/`, `*.bak`) can
  never be committed or shipped. The files themselves were left in place.
- Published test counts synchronized to the real discovery (2694 suite /
  2699 root) in README, README_RU, IMPLEMENTATION_STATUS, RELEASE_CHECKLIST,
  and the vNext index.
- `docs/COMMIT_PLAN_2026-08-20.md`: ten logical commits prepared for the
  ~330-file uncommitted working tree. Nothing was committed; the working-tree
  rule in section 1 still applies.

### Gates for this pass

- Ruff: GREEN. Mypy: GREEN (exit 0).
- Full pytest: **GREEN** (durable job `job-83b79ff5f83a5c2a5a72`: 2826 passed,
  5 skipped, 1847 subtests passed in 838.40s (0:13:58), exit 0).
- Wheel build + wheel contents + release gates + installed-wheel acceptance:
  **GREEN** (wheel job `job-47c0f7976226bff89683` exit 0; gates 21 passed).

### Audit verdict for the release goal

The Quality Economy and Project Intelligence layers the v5 goal asks for
already exist in the working tree and are test-covered: `cost_intelligence.py`
(CI-0..CI-5: ledger, stable-prefix cache, tool-schema deduplication, read
cache, reversible compaction, batch planner, governor), `repo_context.py`
(deterministic fact map: AST + ripgrep + imports, artifact-backed ranked
digest), `task_state.py` (provenance-aware cross-chat state), `checkpoints.py`
(transactional workspace mutations with undo), artifact-first result
envelopes, and the profile-scoped tool catalog (schema snapshot v3).

The remaining distance to Release Candidate is not new subsystems; it is:
committing the working tree (needs explicit approval), live TUI multi-project
and provider acceptance on the host, coverage/security gates, CI parity, and
truthful release documentation.
