# Changelog

## Unreleased

### Fixed — cleanup and a re-judged frame do not erase a visual pass

- The confirmation `3d-game` run was 30/30 on the hidden grader but stopped
  `visual_budget_exhausted`: `visual.check` passed at step 146, the model then
  deleted `_probe.html`, and `completion_identity` changed because it hashed the
  probe. The harness-final check re-judged the same deliverable bytes, returned
  `needs_work`, and the pass became stale. `identity()` now excludes scratch
  artifacts (probe/scratch/tmp files; HTML keeps the page rule), so cleaning a
  diagnostic does not invalidate the deliverable's proof.
- A later `needs_work` on the same current bytes no longer erases an earlier
  vision pass. The checker is a model, not a deterministic measurement; a
  contradictory re-judgment of identical bytes is not a regression. A later
  result that actually saw page/console errors still wins, and a fail after the
  bytes changed is still active.
- `visual.check` results with `unavailable`/`method: limit` are the budget
  speaking, not a verdict about the page: they leave visual evidence missing but
  no longer become "active failed check".
- Scratch artifacts also owe no structural proof of their own.
- The fourth `3d-game` run was 30/30 but ended unverified: visual.check passed
  with only cosmetic notes, the model edited the rail colours anyway, the bytes
  changed and the later check returned needs_work. A pass result now labels its
  notes as optional and says plainly that an edit invalidates the pass.
- The third `3d-game` run on this fix was verified at 1.69M / 54 / 535 s but
  29/30: its probe asserted `doc.body.dataset === doc.body.dataset`, so a
  `reset()` returning empty obstacles and coins still "passed". The system
  prompt now requires a probe to assert `state().obstacles.length > 0` and
  `state().coins.length > 0` after `reset()`, and says a check that compares a
  value to itself proves nothing.

### Fixed — a source-scope instruction does not owe a test edit

- `completion_gate` read "Changes should be made in the source tree, not in the
  test files" as a request to change tests: the negation came after the change
  verb, and the rule only looked before it. Both `swe-requests` runs therefore
  refused their first `finish` with "explicit test change requested but no test
  file changed" and spent the tail editing tests the task had just put out of
  scope: 7 of 34 calls in `repo-command-test-3`, 16 of 47 in
  `swe-tool-budget-20261030` (826k of its 2.05M tokens).
- Test-change detection now judges one sentence at a time, recognizes modal and
  postfix scope negations ("should not modify tests", "rather than in the
  tests", "outside the test files"), and still owes a test change for an
  explicit "add a regression test".
- Live after the fix: `swe-requests` 0.99M total / 28 steps / 165 s, verified,
  6/6, 29 calls, 191k billable. The only write was `requests/utils.py`; the
  repair tail that edited tests is gone (previously 2.05M / 47 calls / 339k
  billable on the same task).

### Fixed — a scratch probe is not the deliverable

- `default_pages()` returned every root HTML sorted by name, so `_probe.html`
  came before `index.html`. The harness-final visual check judged the scratch
  page, and its failure stayed owed against a correct 3D game. Diagnostic names
  (leading `_` or a probe/scratch/tmp/debug token) are now excluded, and
  `index.html`/`index.htm` sort first.
- A `visual.check` on a scratch page no longer counts as an active failed check
  and can no longer satisfy `missing visual evidence`. The final check now looks
  at the page the task actually ships.

### Changed — the game visual rubric follows the task, not an art direction

- The checker now judges a game by the mandatory visual facts the goal states:
  track/floor, lane lines, rails, player, obstacles, collectibles, HUD, and that
  they are distinguishable. A simple craft, a flat or dark sky, a geometric
  obstacle, a clipped decorative hint or a distant item are not failures unless
  the goal names that style or the defect hides a required element.
- The dark-bottom measurement now requires the lower frame to have no visible
  structure. A neon runner's floor is dark by design; its grid, lane lines and
  rails prove the floor is drawn, so the old brightness-only rule called a
  readable track a missing floor. Live recheck of the last 3D frame: bottom
  structure 10.7%, no render problems.

### Fixed — the run keeps time to prove what it built

- Two 3D re-runs fixed the artifact (26/26 and 30/30 on the hidden grader) and
  then hit 1510 s with `wall_time_limit`: the last visual look and change review
  never ran, so the report said "missing visual evidence" while the grader
  passed. The model's own wall clock is now two minutes shorter; the reserved
  window runs the same final calls a submission gets and reports `verified` only
  if the completion gate is clean.
- Browser calls now warn at 20 and stop at 36 per run. The two 3D runs used 57
  and 61 calls; the interaction is verified long before that, and the remaining
  time belongs to the final proof. `visual.check` and repository reads stay
  available after the browser closes.
- A malformed `repo.apply_patch` or a `tests.run` target outside `tests/` now
  disables that tool after two refusals and names the working alternative
  (`repo.edit_file`/`repo.write_file`, or `checks.run`). `swe-sympy-24213` spent
  seven turns on those two shapes alone.
- Every observation in the last five minutes carries one line: stop exploring,
  fix only a named defect, run the final check and visual review, finish.

### Changed — interaction is guided, not forced by a frame gate

- The system prompt and a one-time observation hint say that a task naming
  restart or hotspot must be exercised with `browser.command` click/press, not
  judged from frames alone.
- Observation and input budgets are separate. A run can reach the observation
  limit and still press a control up to a separate 12-action cap; screenshots
  and re-opens stop, the late click remains possible.
- A hard completion-gate requirement for that click was tried and removed. The
  last `3d-game` run passed the hidden grader 30/30 without a click and was
  refused verification; the external grader, not a frame-based gate, is the
  quality signal. The browser capability and the hint remain.

### Fixed — the visual checker loads the page over HTTP

- `visual.check` used to open the page through `file://`. Chromium blocks a
  module script on that scheme, so the checker saw an empty canvas and a console
  error while the same page, served by the grader, passed 30/30. The checker now
  shares the loopback server with `browser.command`: it serves the repository on
  `127.0.0.1`, loads the page over HTTP, and stops the server after the look.
  `web_check.observe_page(..., serve_root=...)` is the one path; the browser
  tool and the checker use the same confined handler.
- A live regression test renders a page with `<script type="module">` through
  `serve_root` and asserts no console/page errors and a live canvas.

### Fixed — a game's ready state is part of the deliverable

- The system prompt now states that a game's ready/initial state must already
  show the first obstacles and collectibles, created in `reset()`/initialization
  rather than only on the first tick, and that restart restores the same set.
  `3d-game` failed its hidden `items-spawn obstacles=0 coins=0` check while the
  visual checker also reported the empty ready screen; the missing rule was in
  how the model set up game state, not in the browser or the gate.

### Added — the browser can open a page from the repository

- `browser.command open {page: "index.html"}` serves the repository on a random
  loopback port and navigates to that page; `serve {path?}` returns the URL
  without navigating. Before this, the canvas/3D runs tried `open` with a
  `file://` URL and got `permission_denied`: `file` is not localhost, so the
  call demanded NETWORK, and the browser was never allowed to open the page the
  run had just written. Restart buttons and hotspots therefore stayed
  unverified even when the frames looked right.
- The static server binds `127.0.0.1` only, is rooted at the repository, refuses
  paths and symlinks outside it, and stops when the browser session closes. It
  is served by the adapter, not by a new public listener; the capability is
  `browser.read` plus `repo.read`, not `network`.
- The system prompt now says to exercise a named interaction (button, restart,
  hotspot, keyboard) through the browser and then use `visual.check` for the
  visual verdict.

### Added — a direct vision route can use the visual checker

- `karox agent run --vision auto|on|off` now controls `visual.check`. `auto`
  keeps the old behavior: the registry decides for a routed model, and a direct
  `--model/--base-url` route stays off because nothing there declares image
  support. `on` opts a known vision-capable direct endpoint in; without it a
  page task ended as `unproducible_requirement` even when the page passed the
  external grader. `off` disables the checker even for a vision model.
- The arena's Karo arm passes `--vision on` for the web, 3D and game families.

### Fixed — repo.command refusals explain the tool instead of inviting variants

- A shell-shaped `repo.command` call (`command`, `argv`, `cmd`, `shell`) is now
  refused with the correct alternative: `checks.run` for the approved
  verification, `tests.run` for extra tests, `dev.command` for a developer
  command. Batch validation also lists the supported fields and restates the
  one canonical `{"operations": [...]}` shape instead of naming only the field
  the model guessed wrong.
- `web.fetch` records the files it creates. Removing the run's own untracked
  scratch copy is ordinary cleanup, not a destructive edit of the user's source,
  so it no longer stops on a confirmation an unattended run cannot give. In the
  arena run that exposed this, 37 refused `repo.command` calls and 2.03M tokens
  went into deleting two downloaded reference files.
- Consecutive refusals of one tool now carry repair guidance after the second
  and disable that tool for the rest of the run after the sixth. A model can no
  longer spend dozens of steps reshaping the same rejected call.
- A research budget now speaks after 12 model steps without a code change, and
  again at 24: stop collecting references, make the smallest edit, run the
  public check. It only applies to tasks that ask for a change (a read-only
  question, a plan, or maintenance mode is left alone), and downloading an
  upstream reference file does not reset it. It fired at step 12 in all three
  final `swe-requests-6028` runs; the first edit followed at step 14 in the
  first of them.
- Measured on `swe-requests-6028` (same model, task and grader, three final
  runs): failed tool calls 44 → 1–3, model calls 64 → 31–38, tokens 2.03M →
  median 0.88M (range 0.76–1.44M), wall 320 s → 195–284 s, still `verified`
  and still solved by the hidden grader. A fix-only control without the
  research budget removed the failures but stayed at 2.13M and 65 calls; the
  token drop is associated with the budget and the shorter run, not with the
  clearer error text alone. Run-to-run variance is large.
- The visual-budget stop now collects the checker's closing look. The grace
  window allowed one edit after the last verdict but skipped the harness-final
  check, so a page that saw `pass`, then one CSS edit, ended as
  `visual_budget_exhausted` with current bytes nobody had looked at. The stop
  path now judges those bytes once before reporting.

### Fixed — a passing page and a patched file count as proof

- A visual pass on the page that renders the change is browser proof even when
  the page file itself is unchanged. `web-snake` and `web-platformer` edited only
  `game.js`; `index.html` was the scaffold. The gate required the checked page
  to be in `changed_files`, so both correct games ended
  `unproducible_requirement`. The gate now accepts the page when it loads a
  changed script/stylesheet from its directory; a sibling scratch probe is not
  required to be referenced.
- The two-frame probe no longer reports "nothing moved" at all; motion is judged
  from the frames against the goal. `web-data-viz` is a static SVG chart, but the
  checker demanded "breathing pulses" on its markers and spent all six looks on
  them. `web-snake` sits in its `ready` state until a key is pressed, yet the
  same line made the checker require a pulsing food dot. Both stopped unverified
  although their graders passed. The checker prompt now says explicitly that a
  static page and a game at rest are not motion failures, and that a specific
  animation must be named by the goal.
- An atomic patch's `post_state` digests now count as current structural
  evidence. `swe-sympy-24213` removed its scratch code from `si.py` with
  `apply_patch`; the patch compiled and the public suite passed, but the result
  carried no `content_sha256`, so the gate failed it as "missing current
  structural evidence". The digest of every written path is now matched against
  the current bytes.
- A visual `pass` now tells the model to run the final review and finish instead
  of continuing to polish. `3d-viewer` saw a pass, spent about twenty more steps
  on an unrequested pan fix, invalidated the verdict and hit the wall clock.
- A malformed `repo.apply_patch` no longer comes back with the batch JSON shape
  as advice; it names the hunk counts and the single-diff form. The 3D run
  retried the wrong shape twice because the error text pointed at `batch`.
- The "bottom quarter is almost black" measurement now applies only to a page
  that renders a 3D scene. `web-snake` passed 17/17 while the checker reported
  the 2D board's background band on every look; the visual budget ran out on a
  page whose lower margin is not a missing floor. A 3D scene with an unlit or
  undrawn floor still gets the problem.

### Changed — the 3D skill is a general technique guide again

- The shipped `web-3d-scene` skill no longer contains the aquarium scene, the
  clownfish skeleton, the bubble recipe or the "exactly N fish" framing. Those
  were benchmark material; they now live in
  `lab/kernel-duel-20261025/live-bench-20261026/aquarium-recipe.md`. The skill
  keeps the general lessons: composition, lighting, lathe silhouettes, clear
  transparent materials, small-effect calibration, economy and a stop rule.
- `tests/test_skill_scope.py` fails if any of the benchmark nouns or counts
  return to the shipped skill, or if the scope discipline and the validated
  technique sections disappear.

### Docs — development providers are explicit and local

- `docs/DEV_PROVIDERS.md` records the current DashScope compatible-mode route
  (model, 1M context, 384k output, max reasoning) and the previous W&B route.
  Keys stay in the git-ignored `.karox/dev-providers.env` or the owner's
  out-of-repository file; documentation names the environment variables, never
  the values. These routes are development-only and are not shipped defaults.

### Changed — the model is not charged again for work it already did

- A tool call that already ran no longer re-sends its arguments on every later
  request. A whole-document write carries the entire file in its arguments; the
  content is on disk and the tool result names the path, the size and the diff.
  Old completed writes and patches now travel as a small valid-JSON marker,
  while the two most recent keep their arguments so a chunked write can continue
  from its seam. An `edit_file` call is deliberately left alone: its old/new
  strings are the anchors the next edits reuse, and eliding them made the
  aquarium run re-read the file 59 times instead of 14. Failed calls and
  `finish` are never rewritten.
- The proof bookkeeping never reaches the model. `completion_identity` contains
  the whole task text and every file hash; the completion gate reads it from the
  durable result, so the model-facing body of `visual.check`, `git.status` and
  `git.diff` now omits it. Six visual checks had made every later request
  re-send an 8 KB echo of the brief (about 640k prompt tokens in one run).
- `repo.read_lines` returns the window as plain lines under a header with the
  start, the end and the total, instead of a JSON object around a list of
  strings. It is about 9% smaller for a 500-line window, and the model no longer
  reads escaped content or internal hashes and secret flags.

### Fixed — the visual loop stops instead of burning the step budget

- Measured problems keep the first slots in a bounded verdict. A model can
  report five variants of one visible defect and push a broken resize — measured
  on every check — out of the five-issue cap; one run then never fixed it.
- The checker remembers what it reported last time and asks the model, per
  issue, whether the new frames show it fixed. Each look used to start from
  zero, so four consecutive checks repeated "no bubbles" for a page whose
  frames showed a few.
- When the checker budget is exhausted, the next `visual.check` stops the run
  with the honest reason `visual_budget_exhausted`. No further verdict can
  arrive, so no further edit can make the run verified; two runs spent 26 and
  20 more steps editing blind and ended on the step limit with a broken resize.
- The three.js recipe now states the frame-level targets that the checks kept
  missing: a warm lamp must leave a visibly warmer pool on the sand, and a
  bubble stream must show 8-12 mostly white bubbles between the floor and the
  surface at the default camera.
- The recipe also carries a validated clownfish skeleton: a lathe body profile
  (not a scaled sphere), three white bands with black rims that follow the body
  radius, protruding eyes at 0.05 of the body length, a tail fan and a dorsal
  fin, with the proportions and the animation hook. The first run that used it
  produced clean, recognizable fish and a zero-failure external grade.
- A page edited after the check budget is exhausted gets one closing look,
  once per page, instead of a `limit` refusal. The sixth check of one run
  judged a page with a runtime error, the model fixed it, and the fixed bytes
  were never judged: the artifact passed the external grader while the run
  stayed unverified. The same bytes, or a second closing attempt, still get the
  hard limit and the honest stop.
- After the last real verdict the model gets two more calls — the edit turn and
  the closing look — and then the run stops. One run kept editing for 40 more
  steps to the step limit after the budget ran out.
- The checker judges small effects at the size they appear: a bubble stream
  passes on presence and continuity rather than glass-perfect transmission at
  1440 px, and a lamp passes on the warmth it casts on the sand and glass rather
  than on its hood geometry. Consecutive runs had failed visible bubbles as
  "absent" and then as "opaque discs", which the model could not resolve.
- The two 3D effect recipes are validated before shipping: the fish skeleton
  was rendered and inspected, and the bubble material was rendered against a
  blue water background. A verified, zero-failure aquarium run (`karo-14`,
  61 steps, 2.72M tokens) used both.
- A changed HTML file is checked statically for duplicate top-level `const`,
  `let` and `class` declarations, and the mutation observation names the
  browser's own error before a visual check is spent. Two runs lost a check to
  a second `const FISH_L` / `waterVolumeMaterial`. Per-loop bindings such as
  `for (let i ...)` and legal `var`/`function` redeclarations stay silent, so
  every verified artifact in the benchmark reports zero problems.

### Changed — clarity is part of the visual verdict, not decoration

- The checker judged presence and counts, so it passed an aquarium whose
  interior had turned into a pale, milky slab behind frosted-looking glass.
  It now checks clarity first and names the failure concretely: no depth behind
  the glass, gravel and the far wall lost in the same pale tone, a flat sheet
  where water should ripple, opaque balls for bubbles. A frame that still looks
  wrong as a picture cannot pass because every checklist item is present.
- The three.js recipe created that failure: it asked for "depth fog inside the
  tank" and said nothing about depth-tested glass. It now states the clear
  material rules: `depthTest: false` on a glass panel composites it over the
  subject, stacked translucent sheets and an emissive water volume turn the tank
  into a frosted box, and a full-screen `mix-blend-mode` layer is a veil over
  the canvas. It also says to stop changing the look after a pass.
- A vision-passed `visual.check` on the changed page now discharges the
  browser-runtime obligation. It already loads the page in Chromium, records
  console and page errors, measures resize and motion, and judges the frames;
  charging a separate approved browser command for the same fact ended correct
  visual runs as `unproducible_requirement`.
- The visual checker's frame filenames include a per-instance token. A resumed
  session restarts the call counter, so its first frames used to overwrite the
  frames that earned the earlier verdict.
- Vision capability now comes from the routed model instead of the registry
  selection. A run routed to a model marked `vision: false` no longer offers
  `visual.check` just because the user's selected model can see images; an
  `unknown` route still inherits the selection, because the checker degrades to
  measurements when a provider rejects images.

## 6.0.0a3 — 2026-10-26

### Fixed — a visual check that can see the whole brief, and valid compacted calls

- The visual checker saw only the first 2,000 characters of the task, so the
  aquarium brief's fish, gravel, plants, lighting and camera requirements were
  invisible to all six checks. It now receives the whole brief up to 12 KB, and
  a caller's `goal` is a focus appended to the task instead of replacing it.
- The checker prompt treats every visible mandatory sentence as a checklist
  item: exact counts, named objects, required effects and style. It also
  receives the measured render problems, and any measured problem forces
  `needs_work` even when the model says `pass`.
- A full-viewport canvas is now resized to 1000×700 and measured after one
  second. The old path only asked a vision model to notice a resize that had
  happened before its screenshots; a canvas that stayed at its old size is now
  a named, deterministic issue.
- Elided historical tool-call arguments are now a valid JSON object
  (`{"_karo_context_manager":"tool arguments elided"}`). The previous plain-text
  marker made the whole transcript invalid to OpenAI-compatible gateways; a
  long live aquarium run failed deterministically with HTTP 400 at step 31 as
  soon as compaction first touched a tool call. Unit tests now parse the marker.

### Changed — the fixed request cost is now part of the task, not the catalog

- Large, domain-specific tool schemas are advertised only when the task names
  their domain: `web.fetch` needs web/URL/docs/library words, `disk.*` needs
  disk/cleanup words, and the atomic batch form of `repo.command` needs
  batch/move/rename/delete words. Hidden names stay callable and audited, so a
  model that already knows the tool is not refused; an unrelated repair simply
  stops paying ~3 KB of JSON on every request. Image tasks still get
  `web.fetch`, because assets are part of that domain.
- `repo.inspect`, `web.fetch`, `tests.run` and `repo.apply_patch` descriptions
  lost their duplicated guidance. The inspect paragraph in the system prompt
  was compressed from ~1,340 to ~640 characters while keeping the project-map,
  edit_context, greenfield and escalation rules.
- A mutation observation is now the decision-shaped text the model reads:
  `[repo.edit_file path]`, changed/replacements/bytes, the diff, the quick-check
  verdict and the affected-test verdict. Hashes, previous hashes, secret flags
  and evidence ids stay in the durable result and are no longer re-sent in
  every later request. Measured on the ledger fixture at the same step count:
  prompt tokens 95,394 -> 79,352 and wall 37.8 s -> 19.5 s, tests 13/13.
  On the shop fixture: 46,960 -> 32,032 prompt and 6 -> 5 steps.
  The full audit record still contains every field.
- When a failure map parsed, the raw test log attached beside it is capped at
  2.5 KB instead of up to 12 KB: the map already names every failure, file and
  line, and the log is corroboration. A long red check on the ledger fixture
  fell from 6,651 to 3,355 characters of model-visible output while keeping
  the map first and the summary tail. Without a map the previous bound stays.
  The stored result and its byte counts are unchanged.

### Fixed — findings from the first live A/B against a minimal harness

- `tests.run` now launches the project's own Python (a project `.venv`, then
  the interpreter on PATH), not KaroX's environment. A pipx-installed KaroX
  has no pytest there, so every focused rerun used to fail with "No module
  named pytest" while the edit result claimed the affected tests had failed.
- A batched response triggers one advisory rerun, after its last action,
  instead of one per edit. A three-edit response used to run the same suite
  three times and leave two stale red results beside the current one.
- A red advisory run with no parsed failure is reported as "could not run",
  not "failed": a runner problem must not point the model at code that never
  ran.

### Fixed — a budget cannot be silently disabled by missing usage

- `ModelPricing.estimate_known` returns `None` when the prompt or completion
  counter is absent, and the router no longer books an absent usage block as a
  measured `$0.00`. The old zero also kept both gates from ever tripping.
- With `--max-cost`, one unknown response stops the run as
  `budget_exceeded:cost_unknown`; with `--max-total-tokens`, a response that
  reports no counters stops it as `token_unknown`. A resumed session carries
  the marker, so the next request is refused before it is sent.
- The session usage records `cost_status: unknown` and a reason
  (`usage_missing` or `no_pricing`); the CLI prints `costs: unknown (...)`
  and the TUI does the same. A partial measured total is labelled incomplete.
- An engine assistant message now persists its measured cost, cumulative cost,
  currency and pricing version. The session rebuild used to drop them, so an
  engine run on a priced model kept per-step cost events but no session total,
  and an unpriced response lost its unknown marker on the way to the report.
- An engine provider budget refusal is now a controlled stop
  (`stopped/budget`), not `failed/provider_error`.

### Fixed — the protected tail is bounded and actually measured

- The compactor counts tool-call arguments and reasoning blocks, not only
  message bodies. A large `repo.write_file` body was invisible to the estimate
  and was re-sent forever.
- When the mutable middle has nothing left to free, the recent tail is clipped
  in place (roles, order and tool-call ids preserved) instead of being resent
  forever. A short history used to never be compacted at all.
- A clipped body keeps **both ends**: the file's first definitions and the
  failing function at the bottom survive, with the omission marker between
  them. Head-only clipping made a large fresh read lose exactly the code the
  task pointed at.
- Live proof with a deliberately small window: a 345 KB module whose bug sits
  in its last function was run with `--context-window 16000`. The 50 KB read
  was compacted in place to ~2 KB (marker present, first lines and the last
  `total()` function still readable), `over_budget_tokens` was 0, and the run
  verified in 4 steps / 24.8k prompt tokens.
- Historical tool-call arguments are replaced by a marker once their body is
  clipped or dropped; the paired tool result keeps its id, so the transcript
  stays valid for every provider.
- The engine budget subtracts the output ceiling from the window, and a history
  that cannot be brought under budget reports `over_budget_tokens` plus a
  `context_floor_exceeded` warning instead of claiming a successful compaction.
- A read of a file is marked stale once that same file has been edited later.
  The body is clipped to 1.2 KB with a marker that says exact lines need a
  fresh read; the durable session record keeps the full result. Live proof: a
  50 KB read of the 345 KB module was 1,199 characters at the next request, so
  the prompt fell from 22.7k to 6.0k tokens; the run still verified in 4 steps
  and 39.7k total prompt tokens (about 13k less than keeping the read).

### Fixed — audit of the check map, the briefing and resume

- pytest failure maps now take path and line from the same frame: an exception
  raised in the module under test used to be reported as the test file with the
  source line. The rerun hint still targets the test's node id.
- A credential echoed by a check is redacted in the durable briefing, and the
  W&B key shape is a known secret pattern. The briefing also caps a long check
  argv and each failure place, so a 5,000-character inline check cannot push
  the failure line out of the block.
- A check result from before the latest edit is marked "predates the latest
  edit"; `web.fetch` counts as a change for the review line; a malformed stored
  count no longer raises in the renderer.
- cargo's `error: test failed` summary and a go panic inside a failed test are
  no longer counted as second failures.
- Repeat detection compares every distinct failure, not only the eight that
  fit in the rendered map.
- Focused-target selection skips symlinks and oversized test files, matches
  `from pkg import name`, and scans vitest file headers in one pass instead of
  quadratically.
- A finish batched with other calls is refused and the other calls still run;
  previously the submission aborted the batch, dropped the later work, and the
  report could claim a change that never happened. finish must now be alone,
  and the engine contract says so.
- An obligation no approved command can produce stops the run with
  `unproducible_requirement` instead of rerunning a passing check and asking
  for an impossible repair. The report names the gap.
- Resume gives the configured step limit to the new run (it used to be
  exhausted by the session's previous calls), resets the format-error budget
  for a repair turn, and rebuilds the transcript with the task before the first
  assistant and every synthesized tool result next to its call.
- The failure map is parsed from the unelided capture (up to 2 MB), so a
  failure in the middle of a >1 MB log is still named instead of being lost by
  the display elision.

### Added — an honest final report

- A stopped run's report now carries `unmet` (the completion-floor reasons
  still open) and `failure_map` (the parsed failure map of the last approved
  check that failed). The human CLI output prints both, and the TUI result
  card shows the reasons and up to five failure lines. A user no longer has to
  open the session directory to learn that the approved check failed and which
  test broke; a verified run carries neither field. This is reporting only:
  the report still cannot make a run verified.

### Fixed — provider robustness after the live route audit

- **Usage metadata can no longer discard a complete answer.** A gateway may
  spell an absent count as `null` or send a whole number as a float; those are
  now treated as absent/valid. A usage-only terminal chunk without `choices`
  is accepted, and a null field no longer raises after the user has already
  seen the streamed answer.
- **DeepSeek cache accounting works.** `prompt_cache_hit_tokens` and
  `cache_read_input_tokens` are recognised beside OpenAI's
  `prompt_tokens_details.cached_tokens`. Without this the live W&B route
  reported the full prompt at full price and hid the cache evidence.
- **Repeated tool-call identities are not concatenated.** A gateway that
  repeats the complete id/name on every delta produced `call_1call_1` and
  `read_fileread_file`, which passed length validation and addressed a tool
  that does not exist. Exact repeats are ignored; genuine fragment splits
  still concatenate.
- **The reduced retry is per model and only remembered after it succeeds.**
  A rejected `reasoning_effort` on one model no longer disables the dial for
  every model on the same provider, and a failed reduction is not paid for
  again. The reduced shape also drops `stream_options` (older Azure versions
  and proxies reject it) and `tool_choice: auto`, while keeping `required`
  and the output cap.
- **A dead key or a broken endpoint falls back.** `AUTHENTICATION`,
  `PERMISSION` and pre-output `MALFORMED_RESPONSE` now join the router's
  fallback set: they describe route 0, and another configured route may still
  serve the request. `stream()` still refuses to fall back after the first
  emitted delta.

### Fixed — provider reasoning-effort compatibility

- A model whose gateway uses a different reasoning-effort vocabulary no
  longer fails the run. When a chat request that carried a reasoning dial is
  rejected with HTTP 400, the transport retries once without the optional
  tuning fields (reasoning, temperature, cache key, parallel-tool-calls) while
  keeping the model, messages, tools and output cap unchanged, and remembers
  the reduced shape for the rest of the session. W&B DeepSeek V4.1 Flash
  rejects `medium` while accepting low/high/xhigh/max/none; the first live run
  failed at step one, and after the change the same task verifies at the
  default effort (step 1: two attempts, steps 2-5: one). The error body is
  still never read, so nothing it might echo can reach a log.

### Added — durable state across compaction

- **Compaction no longer erases what the task requires.** The engine compacts
  by clipping and then replacing old messages with a marker; after a few
  events the model could no longer see the requirements, the files it had
  changed, or the test that failed. `state_briefing.py` now builds a short
  snapshot from the session record — active requirements, the model's open
  plan, changed files, the latest approved check and its parsed failure map,
  and whether `git.diff` ran after the last edit — and appends it to the task
  message, which the compactor never touches. The block is rebuilt on each
  compaction event, replaces itself instead of stacking copies, and is only
  present when a compaction actually fired. Every line is recorded state, not
  a model summary; the block says it is a snapshot and that later messages are
  newer, so a stale "check failed" line cannot masquerade as current truth.

### Added — check intelligence

- **A failing check now arrives as a failure map, not as the tail of a log.**
  `check_report.py` parses pytest, unittest, Jest/Vitest, node:test, go test,
  cargo test and cargo build errors, ruff, mypy and tsc into runner, test id,
  file, line, column, code and a one-line message. The map is extracted from
  the unelided streams before the middle of a long log is cut, so a suite with
  many failures still names every file and line the model needs. The same map
  is prepended to the tool result the model reads, and a failed `tests.run`
  gets it too.
- **stderr is part of the check result.** Jest and cargo print their failures
  to stderr; reading only stdout made a red run look silent. The model now
  sees both streams, labelled.
- **The repair turn names the exact place and a copy-ready rerun.** A failure
  summary carries `path:line`, the failing test id and
  `tests.run suite=focused targets=[...]` for the test files that failed,
  instead of a bounded tail of stdout. A second identical failure set is
  labelled as unchanged ("the last change did not alter it"), so a repair turn
  cannot mistake a repeat for progress.
- **Focused tests derive their own targets.** `tests.run` with
  `suite=focused` and no targets used to refuse the call. It now selects the
  test files under `tests/` that conventionally match or import the modules
  this session changed, reports `targets_source: "session_changes"`, and lets
  every path be validated again by the runner. The approved full command
  remains the only evidence; a focused run is fast feedback.
- **A repair edit answers its own question.** While a check is failing, a
  successful edit now reruns exactly the failing test files (or node ids) from
  the map and attaches the verdict to the edit result the model reads — so the
  turn the model used to spend asking "did that fix it?" is gone. The run is
  bounded (4 targets, 20 s), once per changed content, and advisory: it never
  sets `verification_eligible` and is not recorded as a tool entry.

### Added

- `karox agent run --browser-plain-credentials` lets the browser fill
  password/token fields with throwaway test credentials supplied in the task;
  values stay redacted. Without it nothing changes: the default surface still
  refuses secret fills and named-key submits remain available. A
  single-character `press` now passes the same secret-field gate as `fill`
  (it writes text), closing a character-at-a-time bypass; `press` also
  accepts punctuation keys and normalizes common spellings (`ctrl + enter`,
  `return`, `esc`, arrow names). `get_text` without a selector returns the
  whole page's text instead of refusing.
- The default engine run now lets the browser save a download it was asked
  for. `browser.command {action: download_start}` was implemented and tested
  in the adapter but the agent-run policy kept `downloads=False`, so a browser
  task whose deliverable is a file could not produce one. Files land in the
  KaroX artifact store (content-addressed, 25 MiB cap), never the repository,
  are not executed, and the navigation that produces them stays policy-gated;
  the default policy remains loopback-only, headless and non-detaching. The
  size ceiling is now checked on the saved temp file before the bytes are read
  into memory.
- The system prompt now reads the precomputed project map first: when it
  already names the likely implementation files and tests as a small, specific
  set, the model reads those files directly instead of spending a turn on
  `repo.inspect` to discover what the map already states. Measured on the A/B
  fixture: 4 model calls instead of 5, 15-32% fewer input tokens, both runs
  verified.
- The finishing harness no longer re-runs a full approved check the model
  already passed on the current bytes. The fallback that produces a check when
  the floor names an obligation without a command used to fire for any missing
  proof, including only the final `git.diff` review, so a task that verified
  itself paid for the whole suite twice.
- The engine contract allows independent tool calls to be batched in one
  response (the product prompt already asked for this, and the contract read
  last said "prefer exactly one", so the model serialised work and spent
  turns). One unparseable call inside a batch now returns an observation for
  that call id instead of discarding the valid calls beside it; a lone
  unparseable call keeps the format-error recovery.

### Added

- Browser observations and desktop reads now count as answer evidence. A pure
  research or browser task can be reported verified from its own successful
  `snapshot`/`get_text`/`download_start` observations, and a desktop task from
  its `capture`/`inspect_ui`/`list_windows` reads; `answer_basis` names the
  action and the artifact/sha256 when there is one. The finish tool's answer
  is now the model's final message for that check, so a terse run that never
  wrote prose can still verify. Before this, a browser-only or desktop-only
  task always ended `no_changes` however well it went.
- The agent-run surface has end-to-end coverage of both race domains with a
  scripted model: the fixture site login with test credentials, submit, then
  a download whose artifact bytes are checked by sha256; and an owned
  background desktop window driven through list, attach by window id, inspect,
  type and capture, with the EDIT control read back independently.

### Removed

- The v5 `LegacyAgentKernel` loop and the `--legacy-loop` flag. After the
  kernel duel (independent SWE-rebench + live tasks) the vendored karo-core
  engine is the only kernel; `AgentKernel(..., legacy=True)` now raises. The
  seven proof-state readers both loops shared live on in the new
  `session_analysis.py` (evidence chain, answer basis, step count, safe
  arguments, unresolved checks, attempted mutation, last provider message).
- The legacy loop's economy modules: `cache_scheduler.py`,
  `cost_intelligence.py`, `semantic_shadow.py`, `context_compiler.py`,
  `tool_universe.py`, `base_probe.py` (~9.2k lines total with the loop).
- The vendored engine's unused surface: docker/singularity/extra
  environments, the alternative model backends (openrouter, portkey,
  requesty, litellm text/response variants), `agents/interactive.py`,
  `config/` except `default.yaml`, and `run/` (the mini-swe-agent CLI and
  benchmark harnesses). The vendoring is now exactly the product's import
  closure plus `models/test_models.py`; `check_tree.py` freezes that set.
- Dead interface kwargs on `EngineKernel` (`economy_mode`,
  `lossless_context_rewrite`, `render_feedback`, `domain_notes_enabled`,
  `repeat_read_suppression`, `semantic_shadow`, `base_probe_enabled`).
- The detached MCP/subagent block (`engine_mcp.py`, `subagents.py`,
  `core_engine.py`) and its tests: no product entry point reached it, and MCP
  rides the Core runtime's typed tools. The vendored engine model layer
  (`litellm_model.py` and its utils) went with it; the fenced-command salvage
  lives in `engine_salvage.py`. `litellm` and `tenacity` left the runtime
  dependencies.
- Dead seams and data: the checkpoint auto-guard and its factory, the
  `mutation_reviewer` hook, the worker browser path, the unused
  `BrowserApprovalBroker`, the v5 hosted name catalogue, the dead skill
  selection/policy helpers, `skill_notes/`, the v7 prompt assembler and its
  profile files.

### Fixed

- Every Core tool now passes one argument validator. The built-in validator
  did not know the `integer` type, so the desktop tools' integer arguments
  (window ids, click/scroll coordinates, limits, max nodes, pace) were refused
  before their handlers ran: the computer-use benchmark could not click
  anything. The same code now enforces the constraints the schemas already
  declared (enum, minimum/maximum, string and array bounds, pattern), names
  the accepted arguments on a mismatch, and flattens an unambiguous
  `payload`/`arguments` wrapper that models copied from `browser.command`.
  Desktop tool descriptions now say arguments are top-level and explain that
  `app_id` is the caller's label while `window_id`/`title_contains` choose the
  window.

- `karox bridge connect/start/restart` spawned a child the CLI parser refused:
  the launcher sent `--protocol`, `--saved-profile-name`, `--project`,
  `--default-project-id` and `--allowed-redirect-hosts`, none of which the
  `bridge serve` subparser defined. Both parsers now share one
  `add_serve_arguments()` definition, and a test parses the launcher's exact
  argv with the CLI parser.
- The live event channel dropped events silently: `AgentEvent` had no
  `reasoning_summary_delta` field, and one constructor failure muted the whole
  observer. The field exists, `COMPACTED` writes `summary`, and a malformed
  event is dropped without detaching the observer.
- `SYSTEM_PROMPT` demanded `more_semantic_changes`, which no tool schema has,
  and told the model to answer without a tool call while the engine contract
  calls that a format error. Both instructions were removed.
- `skill.run` had no risk mapping, so every advertised skill call was
  classified unknown-and-high and refused at CONFIRM. It now maps to
  `process.run_dev` (workspace consequence).
- The TUI passed its own detected checks as `--verification-command`, which
  the CLI treats as explicit user approval. Detection is now display-only;
  only `/verify` sends an approved command.
- `tests.run` results claimed `verification_eligible: True` even though only a
  user-approved `checks.run` may prove a change. The flag is gone and a test
  pins that `tests.run` output is never evidence.
- CLI subcommand handlers ended in implicit default branches; an unhandled
  command name would have executed another command's code. Each group now
  refuses an unknown name.

### Changed

- The TUI palette is the v5 rc4 warm set (gold `#d4b676`, amber `#c6a56b`,
  olive muted `#8a7e6a`, ground `#0e0c08`, panels `#1a1712`), replacing the
  cool slate palette.
- The package is 69.1k lines / 137 modules (from ~88k); the line-count guard
  dropped from 90k to 80k. The CLI parser, tool schemas, agent contract,
  engine provider bridge, browser probes and Core types are separate modules.
- `docs/ARCHITECTURE.md` gained a one-line-per-module map; MIGRATION, PARITY,
  DEVELOPMENT, README, AGENTS and TUI.md describe the current tree.

## 6.0.0a2 — 2026-10-25

### Changed

- `karox agent run` (and therefore the TUI backend, which spawns it) now
  executes the vendored karo-core engine (`src/karox/engine`) as the default
  runtime. The new `engine_kernel.py` wraps it: models are reached only
  through the KaroX provider adapters, every shell action passes the
  capability policy, the no-push/no-publish/no-remote-transport invariants,
  repository-confinement checks and the consequence engine (confirmation-class
  actions fail closed as observations), and the harness runs the approved
  verification commands itself after the engine submits. The report carries an
  `engine` identity block (name, vendored version, implementation) so the
  executing loop is provable in-band — the race harness reads exactly this
  block — and the engine run also writes its own trajectory artifact under
  the session's runtime dir (`vnext/sessions/<id>/engine/trajectory.json`,
  tagged with the `karox.engine_kernel.EngineAgent` implementation), which
  the `--legacy-loop` path never writes. The v5 `AgentKernel` remains
  available behind `--legacy-loop` and is unchanged.
- The line-count guard rose from 70k to 80k: the engine vendoring seeded at
  4d8d5d9 already put the tree at ~74.8k against the old bound. The merged
  browser (~6.5k) and computer-use (~6.2k) surfaces then moved it to 90k; the
  merged tree is ~87.9k.
- The default engine agent run (and the TUI backend, which spawns it) now
  registers the browser and desktop surface tools next to the built-in ones
  through the new `karox.engine_tools` wiring: `browser.command` (loopback-only,
  headless, no downloads by default; external navigation still needs the
  network capability) is advertised to write-profile sessions, and the 15
  `desktop.*` tools are advertised to elevated sessions only. Every call flows
  through the same capability policy, Smart-Stop risk gate and audit trail as
  the built-in tools; `CoreRuntime.register_tool` is the construction-time
  registration seam. The risk vocabulary gained explicit `desktop.read` /
  `desktop.input` / `desktop.launch` kinds and explicit grades for the 13
  previously fallback-graded browser actions.
- `workspace_worker.execute_browser_command` (the hosted path) now runs the
  real browser engine behind the runtime's capability policy through a lazily
  attached loopback `BrowserToolRuntime`, instead of refusing every non-`app.*`
  action; `app.*` still forwards to the desktop facade.

### Added

- `--legacy-loop` flag on `agent run`.
- `scripts/race_task.py`: identical-task race runner with isolated
  config/runtime, secret redaction, independent-check verdicts and a
  fairness-aware `compare` subcommand.
- `karox.engine_mcp`: MCP tools for the karo-core engine loop. The
  `EngineMcpAdapter` binds a session's `allow`-selected MCP tools (stdio and
  Streamable HTTP) and exposes them both as a typed Python API (`call_tool`
  with `EngineMcpCallResult` results, `McpProgressEvent` progress, mutation
  lease and idempotent replay) and as an in-band `mcp list` / `mcp call`
  shell command composed into the engine's `command_runner` via
  `build_engine_command_runner`.
- MCP client calls now forward remote progress notifications to an optional
  progress sink.

## 6.0.0a1 — 2026-10-25

First clean release line. The agent kernel, policy, providers, sessions, MCP and
skills are a curated copy of v5; the command surface, terminal client and compact
hosted bridge are new.

### Added

- Compact terminal client (`tui/`): streaming markdown answers, one-line tool
  cards, reason-only-on-request, model and session pickers, provider setup, MCP
  panel, `/doctor`, `/bridge`, confirmation dialogs, RU/EN catalogue and a
  single stylesheet.
- `karox agent run --stream-json`: typed progress events as one JSON document
  per line, then one final result document. This is the client's only runtime
  boundary.
- Compact CLI for `agent`, `session`, `provider`, `model`, `mcp`, `bridge`,
  `skill`, `credential`, `doctor`, `status` and `paths`.
- Inbound hosted bridge for ChatGPT Web, Claude Web and HyperAgent:
  OAuth discovery/DCR/PKCE/refresh, Streamable HTTP MCP, explicit tool allowlist,
  loopback listener, Tailscale Funnel ownership, saved profiles and credential
  management. Existing v5 saved profiles and keyring entries are read unchanged.
- Provider model discovery (`model discover`), multi-route fallback, privacy
  preflight and hard token/cost ceilings.
- Bounded project context (instructions, environment, dependency-aware map) and
  relevant local memory, with opt-outs.
- Plan/Ideate durable artifacts, image attachments and visual-checker injection
  where the model and Chromium support it.
- `doctor`/`status` health surfaces and the short daily command forms
  (`models`, `providers`, `sessions`, `skills`, bare groups).
- Release tests: provider transports, registry and pricing, MCP client and CLI
  lease/replay, policy/risk, session idempotency, OAuth security, hosted
  allowlist/idempotency, Tailscale route ownership, TUI pilot and tree
  contracts.

### Changed

- `provider setup` reuses an existing saved credential, exempts loopback
  endpoints from the key requirement, and verifies a new endpoint with a
  minimal request before storing a key.
- Verification commands discovered from a repository are suggestions. They are
  not accepted as evidence unless the user passes
  `--accept-discovered-checks`; explicit `--verification-command` remains the
  authority.
- `effort-level auto` now resolves from task, project-map and mode signals
  instead of a fixed medium budget.
- A user-approved `checks.run` may execute safe read-only Git subcommands such
  as `git diff --check`; `git push` and other remote mutations remain blocked.
  Root-level `test_*.py` files are detected as pytest verification even without
  a `pyproject.toml`.
- Pricing, route budgets and evidence details are surfaced in the human report.
- The agent refuses drive roots and protected system folders as a workspace,
  reads instruction files without following symlinks outside the repository,
  and ties Bypass to an elevated session profile.
- Mypy is scoped to the new surface (`follow_imports = "skip"`); the copied
  kernel remains outside the declared gate.
- Dependencies now include `starlette`, `uvicorn` and `psutil` for the bridge.

### Removed

- The legacy terminal shell (`tui.py`, `tui_connections.py`,
  `tui_dashboard.py`, `tui_workspace.py`, `tui_onboarding.py`).
- Browser automation/profile surfaces, autonomy runtime, orchestration,
  mission-control, hosted browser/dev-server tools and the OpenAPI bridge.
- Tailscale legacy autostart, Ellipsis, ClickUp/Notion setup and ecosystem
  registries.
- `git.push`, publish, deploy and auth mutations are not reachable from any
  bridge profile.

### Known gaps

See [MIGRATION.md](MIGRATION.md#not-ported-in-600a1). The largest are the
karo-core engine adapter, research subagents, per-session skill selection and
automatic local Git checkpoints.