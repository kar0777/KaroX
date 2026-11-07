# MISSION 14 — VISUAL PIPELINE EFFICIENCY (OFFLINE, GUARDED) (2026-11-07)

**Repository:** `D:\проекты\KaroX-v5`
**Worktree:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win`
**Starting Branch:** `mission-13-1/cache-safety-20261107`
**Starting HEAD:** `a9235ccf948a4b32abeafe61a8a7ee5c2e766546`
**Mission 14 Branch:** `mission-14/visual-pipeline-offline-20261107`

---

## 1. Commit Chain & Changed Paths

No existing Git commits from Mission 13 or Mission 13.1 were amended, rebased, squashed, or rewritten:

1. `a9235ccf948a4b32abeafe61a8a7ee5c2e766546` (`mission-13-1/cache-safety-20261107`): starting HEAD (`docs(audit): record Mission 13.1 cache safety correction and trace reconciliation`).
2. `5f1713c33e249d7c5fcb62db98b1129f114fbd68`: docs-only follow-up commit removing Markdown trailing two-space line endings on lines 3–7 of `docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md` and the trailing EOF blank line on line 150 of `docs/audits/MISSION_13_KV_CACHE_PRESERVATION_20261106.md`.
3. `787a2a1c54b3a6eb2cdf637c93a28f54bef7c1a2`: `visual: add frame-aware adaptive waiting and conditional single-frame static observation`.
4. `b3aef54a723c6cb4bed896dd1844c8c8b5d32c4d`: `test(visual): remove trailing EOF blank line in test_visual_pipeline_offline.py`.
5. Final commit on `mission-14/visual-pipeline-offline-20261107`: adds this audit report (`docs/audits/MISSION_14_VISUAL_PIPELINE_OFFLINE_20261107.md`).

### Pre-Existing Whitespace Verification (§0)

Before Mission 14 edits, running `git diff b6391014..a9235cc --check` exited with code `2`:

```text
docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md:3: trailing whitespace.
docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md:4: trailing whitespace.
docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md:5: trailing whitespace.
docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md:6: trailing whitespace.
docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md:7: trailing whitespace.
docs/audits/MISSION_13_KV_CACHE_PRESERVATION_20261106.md:150: new blank line at EOF.
```

After commit `5f1713c33e249d7c5fcb62db98b1129f114fbd68`, both `git diff --check` (working tree) and `git diff b6391014..HEAD --check` / `git diff a9235cc..HEAD --check` (commit range) exit `0`.

### Changed Files on `mission-14/visual-pipeline-offline-20261107`

- `docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md` (whitespace-only cleanup)
- `docs/audits/MISSION_13_KV_CACHE_PRESERVATION_20261106.md` (whitespace-only cleanup)
- `src/karox/web_check.py` (adaptive waiting, static-document proof, frame-gate outcomes, `observation_fingerprint`)
- `src/karox/visual_check.py` (conditional 1-frame vs 2-frame prompt wording and attachment submission)
- `tests/test_visual_pipeline_offline.py` (11 deterministic, browser-free unit tests)
- `docs/audits/MISSION_14_VISUAL_PIPELINE_OFFLINE_20261107.md` (this report)

---

## 2. Phase A — Grounded Execution Map & Call Graph

### 2.1 Three Visual/Render Execution Paths

```text
Path 1: Approved Verification Subprocess (checks.run -> web_check CLI)
  CoreToolEnvironment._execute("checks_run")
    -> CoreRuntime._checks_run() [spawns NEW OS subprocess: python -m karox.web_check --render]
      -> web_check.main() -> web_check.check(root, render=True)
        -> check_page(page) / check_picture(picture)  [static AST/XML/import checks]
        -> render_page(page)
          -> _run_in_browser_thread(_render_in_browser)
            -> _shared_browser() [launches Chromium ONCE inside the subprocess]
            -> _observe_in_browser(page, settle_ms=0, frames=1, check_resize=False)
          -> render_problems(...)

Path 2: In-Process Visual Quality Check (visual.check tool / harness_final closing look)
  CoreToolEnvironment._execute("visual_check") / EngineKernel._final_verification_calls()
    -> ExtendedCoreRuntime._visual_check()
      -> VisualChecker.__call__(page, goal=...)
        -> web_check.observe_page(page, settle_ms=2000, check_resize=True, serve_root=self.serve_root)
          -> static_server.serve_repository(serve_root) [ephemeral 127.0.0.1 HTTP server]
          -> _run_in_browser_thread(_observe_in_browser)
            -> _shared_browser() [reuses warm Chromium in host karox process]
            -> _observe_in_browser(page, settle_ms=2000, frames=2, check_resize=True, url=loopback_url)
        -> render_problems(...)
        -> VisualChecker._save_frames(page, frames)
        -> VisualChecker._ask(frames, ...) -> provider.complete(ModelRequest(..., images=frames))

Path 3: Independent Entrypoints
  - web_check._screenshot_in_browser(page): standalone 1-frame helper (unused by VisualChecker)
  - browser.command (BrowserToolAdapter -> BrowserSession): interactive Playwright session used by model for open/click/press/evaluate/screenshot, with its own FRAME_PROBE_JS / RESPONSE_FRAME_PROBE_JS in src/karox/browser/pageperf.py
```

### 2.2 Baseline vs Implementation Call Graph inside `_observe_in_browser()`

**Baseline (`a9235ccf`):**
1. `view.goto(url)` + optional `_SVG_FIT_JS`
2. Unconditional `view.wait_for_timeout(RENDER_SETTLE_MS)` (`2500 ms`) on every page (even static SVG/HTML).
3. `_frames_drawn(view, 2, 30_000)` (swallowed timeouts silently, returned `None`).
4. `drawn = _gate(view, "pause")` + `probe = view.evaluate(_PAGE_PROBE_JS)` (unwrapped; any exception raised generic error).
5. `shots.append(view.screenshot())` (Frame 1).
6. For `frames=2`: unconditional `view.wait_for_timeout(settle_ms)` (`2000 ms`) + `_frames_drawn(view, drawn + 2, 20_000)` + `shots.append(view.screenshot())` (Frame 2), even for provably static SVG/HTML.
7. `measured = view.evaluate(_PIXEL_STATS_JS, ...)`.
8. If `check_resize=True` and canvas fills $\ge 90\%$ of viewport: `view.set_viewport_size({1000, 700})`, unconditional `view.wait_for_timeout(1000)` (`1000 ms`), `_frames_drawn(view, drawn + 1, 10_000)`, `view.evaluate(_RESIZE_PROBE_JS)`.

**Implementation (`b3aef54a`):**
1. Static pre-check `static_candidate = is_static_document(page)` (verifies well-formed HTML/SVG and absence of `<script>`, `<canvas>`, `<video>`, `<audio>`, `<iframe>`, `<animate*>`, `<set>`, inline `on*=` handlers, `@keyframes`, CSS `animation:`/`transition:`, `requestAnimationFrame`, and external URLs).
2. If `not static_candidate`: runs full `view.wait_for_timeout(RENDER_SETTLE_MS)` (`2500 ms`).
3. `gate_status = _frames_drawn(view, 2, FIRST_FRAMES_TIMEOUT_MS)` returning explicit status (`"ready"`, `"timed_out"`, or `"gate_unavailable"`).
4. `drawn = _gate(view, "pause")`; if `drawn < 2` on a non-static candidate, marks `gate_status = "still_rendering"`.
5. Runs `_PAGE_PROBE_JS` (which now also checks live DOM for `script,canvas,video,audio,iframe,animate,animateTransform,animateMotion,set` and `document.getAnimations().length > 0`). Wraps probe errors explicitly as `RuntimeError("probe_error: ...")` and routes non-availability `RuntimeError`s in `observe_page`/`render_page` to `{"error": ..., "problems": [...]}` instead of falsely reporting `{"skipped": "playwright_unavailable"}`.
6. Computes `static_proven = bool(static_candidate and not dynamic_dom and gate_status == "ready" and drawn == 0 and not page_errors)`.
   - Fail-closed fallback: if `static_candidate` was true from markup, but live DOM/frame-gate/errors did NOT prove staticness (`not static_proven`), immediately resumes the gate and runs the full `RENDER_SETTLE_MS` (`2500 ms`) stabilization wait before capturing Frame 1.
7. Captures `target_frames = 1 if static_proven and frames > 0 else frames`.
   - For `static_proven=True`: takes 1 screenshot, skips `settle_ms` (`2000 ms`) and second screenshot, and `VisualChecker._ask()` sends 1 `ImageAttachment` with `"One frame follows: captured after the static document rendered."`.
   - For all dynamic or uncertain pages (`static_proven=False`): preserves `RENDER_SETTLE_MS` (`2500 ms`), `settle_ms` (`2000 ms`), 2 screenshots, 2 `ImageAttachment`s, and `"Two frames follow: ..."`.
8. For full-viewport canvas resize (`check_resize=True`): waits on frame-gate readiness `_frames_drawn(view, drawn + 1, RESIZE_FRAMES_TIMEOUT_MS)` first and evaluates `_RESIZE_PROBE_JS`; only falls back to bounded `250 ms` / `750 ms` waits if the frame gate timed out or the canvas has not yet resized to `1000x700`.

---

## 3. Optimization Status Summary

| Optimization | Status | Technical Justification |
|---|---|---|
| **C1. Frame-aware adaptive waiting** | `IMPLEMENTED` | Skips unconditional `2500 ms` + `2000 ms` sleeps when `static_proven` holds; replaces unconditional `1000 ms` resize sleep with frame-gate readiness + fail-closed fallback (`250 ms` / `750 ms`) when canvas resize lags; records `gate_status` (`ready`, `still_rendering`, `timed_out`, `gate_unavailable`, `probe_error`) and `timings`. |
| **C2. Conditional single-frame vision request** | `IMPLEMENTED` | When `static_proven=True`, captures 1 PNG frame, saves 1 artifact file, and sends 1 `ImageAttachment` with accurate prompt wording (`"One frame follows..."`). Dynamic/uncertain pages continue capturing and sending 2 frames (`"Two frames follow..."`). |
| **C3. Cross-process duplicate render elimination** | `DEFERRED` | `checks.run` executes `python -m karox.web_check --render` in a child OS process, whereas `visual.check` runs in-process inside the host agent. Furthermore, `web_check --render` captures 1 frame without loopback server (`file://`) and without resize probing, whereas `visual.check` requires `serve_repository(serve_root)` over HTTP and `check_resize=True`. Reusing a `web_check` CLI render inside `visual.check` would drop HTTP module loading, second-frame motion evidence, and resize verification. We implemented and tested the pure freshness digest helper `observation_fingerprint()` in `src/karox/web_check.py` (binding page bytes, all served repository assets, viewport, query, and render backend), and deferred cross-process caching per §5.C3. |

---

## 4. Phase B & Phase D — RED and GREEN Test Evidence

### 4.1 RED Evidence (Before Production Edits)

Command executed before editing `src/karox/web_check.py` and `src/karox/visual_check.py`:

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -o addopts="" -p no:cacheprovider tests/test_visual_pipeline_offline.py
```

Result (`exit code 1`, `7 failed, 3 passed in 0.31s`):
- `FAILED tests/test_visual_pipeline_offline.py::test_is_static_document_requires_clean_markup_without_scripts_animations_or_external_assets` (`AssertionError: is_static_document helper is missing`)
- `FAILED tests/test_visual_pipeline_offline.py::test_quiescent_static_document_skips_unconditional_fixed_waits_and_second_frame` (`assert [2500, 2000] == []`)
- `FAILED tests/test_visual_pipeline_offline.py::test_dynamic_3d_canvas_retains_stabilization_second_frame_and_resize_probe` (`KeyError: 'static_proven'`)
- `FAILED tests/test_visual_pipeline_offline.py::test_frame_gate_timeout_or_absent_gate_is_recorded_and_never_fabricates_static_pass` (`KeyError: 'static_proven'`)
- `FAILED tests/test_visual_pipeline_offline.py::test_probe_error_fails_closed_and_reports_problem` (`AssertionError: assert 'error' in {'skipped': 'playwright_unavailable'}`)
- `FAILED tests/test_visual_pipeline_offline.py::test_visual_checker_sends_single_frame_and_honest_text_when_static_proven` (`AssertionError: assert 2 == 1`)
- `FAILED tests/test_visual_pipeline_offline.py::test_observation_fingerprint_invalidates_on_page_asset_viewport_query_or_backend_change` (`AssertionError: observation_fingerprint helper is missing`)

### 4.2 GREEN Evidence (After Production Edits)

1. **Visual Pipeline & Web/Browser Offline Suite:**
   ```text
   PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -o addopts="" -p no:cacheprovider tests/test_visual_pipeline_offline.py tests/test_visual_check.py tests/test_web_check.py tests/test_browser_render_backend.py tests/test_pageperf.py tests/test_engine_tool_env_render.py tests/test_state_briefing.py tests/test_instruction_neutrality.py tests/test_prompt_cache.py tests/test_engine_port_context.py tests/test_completion_gate.py
   ```
   Output: **`185 passed in 22.97s` (exit code `0`)**.

2. **Engine Kernel Cache-Safety & Visual Closing-Look Invariants:**
   ```text
   PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -o addopts="" -p no:cacheprovider -k "prompt_cache or rewrite or supersede or compaction or state_briefing or context_budget or closing_look or render_target or render_check or visual_check_looks_at_a_standalone_svg or visual_budget_stop" tests/test_engine_kernel.py
   ```
   Output: **`10 passed, 111 deselected in 14.41s` (exit code `0`)**.

---

## 5. Phase E — Honest Before/After Metrics on Deterministic Mock Fixtures

All measurements below come from the deterministic in-memory `_FakeView` and `_FakeProvider` fixtures in `tests/test_visual_pipeline_offline.py` and `tests/test_visual_check.py`. No real browser or live model was run.

| Metric | Before (`a9235ccf`) | After (`b3aef54a`) | Measurement or estimate? |
|---|---:|---:|---|
| Unconditional wait ms — quiescent static SVG/HTML (`observe_page`) | `4,500 ms` (`2500 + 2000`) | `0 ms` | Measured on mock fixture (`view.timeouts`) |
| Unconditional wait ms — dynamic 3D/canvas with responsive resize (`observe_page`) | `5,500 ms` (`2500 + 2000 + 1000`) | `4,500 ms` (`2500 + 2000 + 0`) | Measured on mock fixture (`view.timeouts`) |
| Unconditional wait ms — dynamic 3D/canvas with lagging resize (`observe_page`) | `5,500 ms` | `5,250 ms` (`2500 + 2000 + 750`) | Measured on mock fixture |
| Frame gate calls / timeout handling | Swallowed (`None`), no status | Explicit `gate_calls` count + `ready` / `still_rendering` / `timed_out` / `gate_unavailable` | Measured on mock fixture |
| Screenshots taken & submitted — proven static SVG/HTML | `2` taken, `2` submitted | `1` taken, `1` submitted | Measured on mock fixture |
| Screenshots taken & submitted — dynamic 3D/canvas/unknown | `2` taken, `2` submitted | `2` taken, `2` submitted | Measured on mock fixture |
| Browser/process launches in this mission | `0` | `0` | Measured (all tests use mocked `_shared_browser` / `observe_page`) |
| Browser error or unavailable handling | Probe `RuntimeError` misclassified as `skipped: playwright_unavailable` | `unavailable`/`skipped` when browser absent; `error` + `problems` on `probe_error` | Measured on mock fixture |
| Existing quality verdicts preserved | 13/13 in `test_visual_check.py` | 13/13 in `test_visual_check.py` + 11/11 in `test_visual_pipeline_offline.py` | Measured |
| Context size of vision request (static vs dynamic) | Always 2 `ImageAttachment`s + `"Two frames follow..."` | 1 `ImageAttachment` + `"One frame follows..."` (static); 2 `ImageAttachment`s (dynamic) | Measured on `_FakeProvider.requests` |
| Cross-process duplicate render attempts | Not deduplicated across subprocess boundary | Deferred (unsafe across `web_check` CLI vs `visual.check` HTTP/resize boundary); `observation_fingerprint` helper added | Architectural analysis + unit test |
| Total fixture wall time (`test_visual_pipeline_offline.py`, 11 tests) | N/A (new suite) | `0.32 s` | Measured by pytest |

*Note on predicted vs measured gains:* In real executions, skipping the second frame and fixed waits on static SVG/HTML tasks (such as `pelican`) is predicted to save `~4.5 s` of wall time and 1 image attachment per `visual.check` call, and `~1.0 s` per responsive resize check on 3D/canvas pages. However, because zero real browsers or live providers were launched in Mission 14, real-world wall-time and token-cost savings remain `NOT_RUN` until validated under the bounded live-browser protocol below.

---

## 6. Protected Quality Contracts & Covering Tests

| Protected Contract | Verification Test(s) |
|---|---|
| Staticness requires clean markup + zero scripts/animations/external assets + 0 drawn rAF frames | `test_visual_pipeline_offline.py::test_is_static_document_requires_clean_markup_without_scripts_animations_or_external_assets` |
| Quiescent static page skips fixed sleeps and captures 1 frame | `test_visual_pipeline_offline.py::test_quiescent_static_document_skips_unconditional_fixed_waits_and_second_frame` |
| Dynamic 3D / canvas / WebGL retains `RENDER_SETTLE_MS`, `settle_ms`, 2 frames, and resize check | `test_visual_pipeline_offline.py::test_dynamic_3d_canvas_retains_stabilization_second_frame_and_resize_probe` |
| Frame-gate timeout, absent gate, or `still_rendering` never fabricates a static pass | `test_visual_pipeline_offline.py::test_frame_gate_timeout_or_absent_gate_is_recorded_and_never_fabricates_static_pass`, `test_still_rendering_outcome_when_gate_returns_fewer_than_target_frames_on_dynamic_page` |
| Probe JS error fails closed with `error` and `problems` (not `skipped`) | `test_visual_pipeline_offline.py::test_probe_error_fails_closed_and_reports_problem` |
| Missing Playwright/Chromium returns `skipped` / `unavailable`, never `pass` | `test_visual_pipeline_offline.py::test_missing_playwright_reports_skipped_or_unavailable_never_pass` |
| Console errors, page errors, and resize failures override a vision `pass` | `test_visual_pipeline_offline.py::test_console_errors_page_errors_and_resize_mismatch_override_vision_pass_Even_on_static_page`, `test_visual_check.py::test_visual_checker_uses_the_full_task_and_forces_resize_needs_work` |
| Vision prompt honesty (`"One frame follows..."` for 1 frame vs `"Two frames follow..."` for 2 frames) | `test_visual_pipeline_offline.py::test_visual_checker_sends_single_frame_and_honest_text_when_static_proven`, `test_visual_checker_preserves_two_frames_and_timing_text_for_dynamic_pages` |
| Freshness digest invalidates on HTML/CSS/JS/image/font/viewport/query/path/backend change | `test_visual_pipeline_offline.py::test_observation_fingerprint_invalidates_on_page_asset_viewport_query_or_backend_change` |
| Per-task budget (`CHECKS_PER_TASK`), previous-issue memory, closing look, and resume frame non-overwrite | `test_visual_check.py::test_a_resumed_checker_does_not_overwrite_earlier_frames`, `test_the_second_look_remembers_the_first_issues`, `test_a_page_fixed_after_the_budget_gets_one_closing_look` |
| Mission 13.1 KV-cache prefix safety, context guard, and instruction neutrality | `test_state_briefing.py`, `test_prompt_cache.py`, `test_engine_port_context.py`, `test_instruction_neutrality.py` |

---

## 7. Static & Hygiene Gate Results

- **`python scripts/check_tree.py`**:
  `check_tree: ok · 162 modules · 87445 lines · no legacy surface` (exit code `0`; `555` lines below the `88,000` ceiling).
- **`python -m ruff check src tests scripts --no-cache`**:
  `All checks passed!` (exit code `0`).
- **`python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui src/karox/web_check.py src/karox/visual_check.py`**:
  `Success: no issues found in 15 source files` (exit code `0`).
- **`git diff --check` and `git diff b6391014..HEAD --check` and `git diff a9235cc..HEAD --check`**:
  All exit `0` with zero output.
- **Proof of Zero Browser / Provider / Network Execution**:
  Every test executed in this mission was statically inspected before invocation. `tests/test_visual_pipeline_offline.py` monkeypatches `playwright_chromium_status`, `_run_in_browser_thread`, and `_shared_browser` with in-memory `_FakeView` and `_FakeProvider` stand-ins; `tests/test_visual_check.py` monkeypatches `visual_check.observe_page` and uses in-memory fake providers; `tests/test_web_check.py` calls only static AST/XML functions (`check_page`, `check_picture`, ` check(..., render=False)`). No `test_browser_live.py`, `test_computer_use*`, or uninspected `test_engine_tools.py` nodes were executed.

---

## 8. Proposed Bounded Live-Browser Validation Protocol (NOT RUN)

For future explicit owner approval, validate the Mission 14 visual pipeline changes in an isolated headless Chromium instance (owned solely by the test process, never touching user browser windows) across four local fixtures:

1. **Static SVG (`pelican.svg`)**:
   - Verify `observe_page` completes in `< 500 ms` after navigation with `static_proven=True`, `gate_status="ready"`, `len(frames) == 1`, and `unconditional_wait_ms == 0`.
2. **Dynamic CSS Animation (`pulse.html` with `@keyframes` and `document.getAnimations()`)**:
   - Verify `is_static_document` and `probe.dynamic_dom` classify the page as dynamic (`static_proven=False`), retaining `RENDER_SETTLE_MS`, `settle_ms`, and 2 frames.
3. **Slow WebGL / Three.js Scene (`aquarium-test.html` under both `KAROX_BROWSER_GPU=1` D3D11 and `KAROX_BROWSER_GPU=0` SwiftShader)**:
   - Measure wall time per `observe_page(..., check_resize=True)` call before and after the adaptive resize wait, verifying that responsive canvas resize avoids the `1000 ms` sleep while preserving pixel statistics (`sampled`, `detail`, `luma_mean`, `luma_std`, `dark_bottom_share`) and resize detection.
4. **Interactive Keyboard Game (`snake.html` in initial `ready` state)**:
   - Verify that `<canvas>` + `<script>` keeps `static_proven=False` (2 frames captured), while identical frames in `ready` state do not fabricate a motion defect.

---

## 9. Recommendation

**Verdict: `ACCEPT`**

Mission 14 C1 (frame-aware adaptive waiting with fail-closed fallback) and C2 (conditional 1-frame vision request for strictly proven static documents) are implemented, verified offline with 195 passing unit tests, and keep the package well within the 88,000-line ceiling (`87,445` lines). Ready for owner review before any live browser or three-arm benchmark validation.
