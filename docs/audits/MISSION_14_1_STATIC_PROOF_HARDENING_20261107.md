# MISSION 14.1 — STATIC-PROOF HARDENING (OFFLINE) (2026-11-07)

**Repository:** `D:\проекты\KaroX-v5`
**Worktree:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win`
**Starting Branch:** `mission-14/visual-pipeline-offline-20261107`
**Starting HEAD:** `623851be8005953b3052c6183b5a5bde8a73d5cb`
**Mission 14.1 Branch:** `mission-14-1/static-proof-hardening-20261107`
**Product + Tests Commit:** `94ce9fe3a1cef129e636153ce3cd797281bb5106`

---

## 1. Git Parent Chain & Changed Files

No existing commits from Mission 13, 13.1, or 14 were amended, rebased, squashed, or rewritten:

1. `a9235ccf948a4b32abeafe61a8a7ee5c2e766546` (`mission-13-1/cache-safety-20261107`): Mission 13.1 final HEAD.
2. `5f1713c33e249d7c5fcb62db98b1129f114fbd68`: docs-only whitespace cleanup in Mission 13/13.1 audit reports.
3. `787a2a1c54b3a6eb2cdf637c93a28f54bef7c1a2`: Mission 14 visual pipeline implementation + offline tests.
4. `b3aef54a723c6cb4bed896dd1844c8c8b5d32c4d`: test EOF whitespace cleanup.
5. `623851be8005953b3052c6183b5a5bde8a73d5cb` (`mission-14/visual-pipeline-offline-20261107`): Mission 14 audit report (starting HEAD for Mission 14.1).
6. `94ce9fe3a1cef129e636153ce3cd797281bb5106`: `visual: harden static-document proof and bound observation_fingerprint walk`.
7. Final commit on `mission-14-1/static-proof-hardening-20261107`: adds this audit report (`docs/audits/MISSION_14_1_STATIC_PROOF_HARDENING_20261107.md`).

### Changed Files in Mission 14.1

- `src/karox/web_check.py`
- `tests/test_visual_pipeline_offline.py`
- `docs/audits/MISSION_14_1_STATIC_PROOF_HARDENING_20261107.md`

---

## 2. Phase A & B — Reproduced Root Cause & Three False-Static Cases

### 2.1 Distinction Among Static States

In `_observe_in_browser()` (`src/karox/web_check.py`), three distinct states govern whether the single-frame fast path may be taken:

1. **Static candidate (`is_static_document(page)`)**: pure static inspection of the document's own markup on disk before navigation.
2. **Observationally quiescent (`gate_status == "ready" and drawn == 0 and not dynamic_dom`)**: instantaneous state of the browser page at the moment `_PAGE_PROBE_JS` and `_gate(view, "pause")` run.
3. **Static proven (`static_proven = True`)**: conjunctive proof that the document and all of its visual dependencies cannot change over time, authorizing `_observe_in_browser()` to skip `RENDER_SETTLE_MS` (`2500 ms`), skip `settle_ms` (`2000 ms`), capture 1 frame instead of 2, and send 1 image attachment in `VisualChecker._ask()`.

As noted in the independent review, observational quiescence at a single instant does **not** prove staticness if the document loads uninspected local stylesheets, animated raster/vector images (`.gif`, animated `.webp`/`.png`, external/local `<image>`), or CSS longhand animations (`animation-name`, `transition-property`, etc.).

### 2.2 Reproduced Defect on `623851be` (RED Evidence)

In `623851be`, `_DYNAMIC_MARKUP` only matched:
- `<script|canvas|video|audio|iframe|object|embed|animate(?:Transform|Motion)?|set>`
- `on[a-z]+=`
- `@keyframes`, `@import`
- shorthand `(?:animation|transition)\s*:`
- `requestAnimationFrame`
- remote `(?:href|src)=["'](?:https?:)?//` and `url((?:https?:)?//)`

Consequently, running `pytest` on `tests/test_visual_pipeline_offline.py` before editing `src/karox/web_check.py` reproduced all three false-static classifications:

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -o addopts="" -p no:cacheprovider tests/test_visual_pipeline_offline.py
```

Output (`exit code 1`, `2 failed, 12 passed`):
1. `linked_css.html` (`<link rel="stylesheet" href="style.css">` where `style.css` defines `@keyframes spin` and `animation: spin 2s infinite`): `is_static_document` returned **`True`** (expected `False`), and mocked `observe_page` with `drawn=0, dynamic_dom=False` produced `static_proven=True` and captured only 1 frame.
2. `gif.html` (`<img src="motion.gif">`): `is_static_document` returned **`True`** (expected `False`).
3. `longhand_html` (`<style>.spinner { animation-name: spin; animation-duration: 2s; }</style>`): `is_static_document` returned **`True`** (expected `False`).

```text
E       assert [True, True, True] == [False, False, False]
E         At index 0 diff: True != False
```

---

## 3. Phase C — Minimal Fail-Closed Implementation

### 3.1 Hardened Static-Proof Boundary (`src/karox/web_check.py`)

We hardened `_DYNAMIC_MARKUP`, `is_static_document()`, and `_PAGE_PROBE_JS.dynamic_dom` to fail closed on any external or local dependency, media element, or CSS animation/transition property:

1. **Tag exclusions in `_DYNAMIC_MARKUP` and `_PAGE_PROBE_JS.dynamic_dom`:**
   - Rejects `<script>`, `<canvas>`, `<video>`, `<audio>`, `<iframe>`, `<object>`, `<embed>`, `<link>`, `<img>`, `<picture>`, `<source>`, `<image>`, `<feImage>`, `<foreignObject>`, `<animate>`, `<animateTransform>`, `<animateMotion>`, `<set>`, and `<meta http-equiv="refresh">`.
   - Any HTML or SVG referencing an external stylesheet (`<link>`) or raster/external image (`<img>`, `<picture>`, `<source>`, SVG `<image>`, `<feImage>`) is conservatively classified as non-static (`False`), retaining the full two-frame capture and stabilization waits.
2. **CSS animation, transition, and @-rule exclusions:**
   - Rejects `@keyframes`, `@import`, and `@font-face`.
   - Rejects both shorthand and **all longhand** CSS animation and transition properties: `\b(?:animation|transition)(?:-[a-z]+)?\s*:` (catching `animation-name:`, `animation-duration:`, `animation-delay:`, `transition-property:`, `transition-duration:`, etc.), as well as `offset-path:` and `will-change:`.
3. **Attribute and `url(...)` resource exclusions (while preserving internal `#fragment` references):**
   - Rejects any `src=` or `srcset=` attribute.
   - Rejects any `href=` or `xlink:href=` whose value is not a same-document fragment (`#id`), so `<a href="#top">` and SVG `<use href="#c">` remain eligible for the static fast path while `<link href="style.css">`, `<image href="sprite.png">`, and `href="data:..."` fail closed.
   - Rejects any CSS `url(...)` whose target is not an internal SVG paint-server/clip/filter fragment (`url(#g)`), so `fill="url(#g)"` in standalone SVGs remains eligible for the static fast path while `background-image: url('anim.webp')` and `url(data:...)` fail closed.
4. **Symlink rejection:**
   - `is_static_document()` returns `False` if `page.is_symlink()` is true.

### 3.2 Proof Boundaries Summary

| Document / Resource Category | `is_static_document()` | `observe_page()` Behavior |
|---|---|---|
| Standalone SVG with shapes, text, gradients (`url(#id)`), `<use href="#id">`, static `<style>` | **`True`** | Fast path eligible (`0 ms` fixed wait, `1` frame if `drawn==0` and `gate_status=="ready"`) |
| Self-contained HTML with inline static `<style>`, typography, tables, `<a href="#id">` | **`True`** | Fast path eligible (`0 ms` fixed wait, `1` frame if `drawn==0` and `gate_status=="ready"`) |
| HTML with `<link rel="stylesheet" href="style.css">` (local or remote CSS) | **`False`** | Fail-closed: `2500 ms` + `2000 ms` waits, `2` frames |
| HTML/SVG with `<img>`, `<picture>`, `<source>`, `<image>`, `.gif`, `.webp`, `.png`, `data:` | **`False`** | Fail-closed: `2500 ms` + `2000 ms` waits, `2` frames |
| HTML/SVG with `animation-*` or `transition-*` longhands, `@keyframes`, `@import`, `url('...')` | **`False`** | Fail-closed: `2500 ms` + `2000 ms` waits, `2` frames |
| HTML/SVG with `<script>`, `<canvas>`, WebGL, `<video>`, `<animate*>`, `<set>`, `on*=` | **`False`** | Fail-closed: `2500 ms` + `2000 ms` waits, `2` frames |
| Missing, unreadable, oversized (`> 8 MB`), symlinked, or malformed HTML/SVG | **`False`** | Fail-closed |

### 3.3 Secondary P1 Fix — Bounded `observation_fingerprint()` Enumeration

In Mission 14, `observation_fingerprint()` called `sorted(root.rglob("*"))` before checking `_REFERENCE_MAX_FILES` (`500`), which could traverse an unbounded directory tree or follow symlinks outside `root`.

**Fix in `src/karox/web_check.py` (`IMPLEMENTED`):**
- Replaced `root.rglob("*")` with `os.walk(root, followlinks=False)`.
- Prunes `_REFERENCE_SKIP_DIRS` (`.git`, `node_modules`, `__pycache__`, `.venv`, `venv`) and any symlinked subdirectories before descending.
- Rejects symlinked pages or asset files fail-closed (`return None`).
- Verifies `resolved.relative_to(root)` for both `page` and every enumerated asset.
- Enforces both `_REFERENCE_MAX_FILES` (`500`) **during** iteration (stopping immediately when `seen > _REFERENCE_MAX_FILES`) and cumulative `_REFERENCE_MAX_BYTES` (`4,000,000` bytes).
- Tested offline in `test_observation_fingerprint_rejects_symlinks_and_bounds_enumeration`.

---

## 4. Phase D — Bounded Offline GREEN Validation & Before/After Synthetic Evidence

### 4.1 Before/After Synthetic Evidence Table

| Synthetic Fixture | Before (`623851be`) | After (`94ce9fe3`) | Mocked `observe_page` Frames & Waits (`94ce9fe3`) |
|---|---:|---:|---|
| `<link rel="stylesheet" href="style.css">` (local CSS with `@keyframes`) | `True` (false static) | **`False`** | `static_proven=False`, `2` frames, `[2500, 2000]` ms waits |
| `<img src="motion.gif">` | `True` (false static) | **`False`** | `static_proven=False`, `2` frames, `[2500, 2000]` ms waits |
| Inline `animation-name: spin; animation-duration: 2s` | `True` (false static) | **`False`** | `static_proven=False`, `2` frames, `[2500, 2000]` ms waits |
| Inline `transition-property: opacity; transition-duration: 1s` | `True` (false static) | **`False`** | `static_proven=False`, `2` frames |
| `<picture><source srcset="hero.webp"><img src="hero.png"></picture>` | `True` (false static) | **`False`** | `static_proven=False`, `2` frames |
| SVG `<image xlink:href="sprite.png"/>` | `True` (false static) | **`False`** | `static_proven=False`, `2` frames |
| CSS `.hero { background-image: url('anim.webp'); }` | `True` (false static) | **`False`** | `static_proven=False`, `2` frames |
| `<meta http-equiv="refresh" content="1">` | `True` (false static) | **`False`** | `static_proven=False`, `2` frames |
| Clean SVG (`pelican.svg` with `<linearGradient>`, `fill="url(#g)"`, `<use href="#c">`) | `True` | **`True`** | `static_proven=True`, `1` frame, `[]` (`0 ms`) fixed waits |
| Clean static HTML (`static.html` with inline `<style>` and `<a href="#top">`) | `True` | **`True`** | `static_proven=True`, `1` frame, `[]` (`0 ms`) fixed waits |

### 4.2 Executed Offline Test Commands & Results

1. **Direct Visual Pipeline Offline Suite (14 tests):**
   ```text
   PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -o addopts="" -p no:cacheprovider tests/test_visual_pipeline_offline.py
   ```
   Result: **`14 passed in 0.27s` (exit code `0`, `0` warnings)**.

2. **Combined Browser-Free Visual, Web, Prompt-Cache & Completion-Gate Suite (188 tests):**
   ```text
   PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -o addopts="" -p no:cacheprovider tests/test_visual_pipeline_offline.py tests/test_visual_check.py tests/test_web_check.py tests/test_browser_render_backend.py tests/test_pageperf.py tests/test_engine_tool_env_render.py tests/test_state_briefing.py tests/test_instruction_neutrality.py tests/test_prompt_cache.py tests/test_engine_port_context.py tests/test_completion_gate.py
   ```
   Result: **`188 passed in 24.25s` (exit code `0`, `0` failed, `0` skipped)**.

3. **Engine Kernel Cache-Safety & Visual Closing-Look Subset (10 tests):**
   ```text
   PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -o addopts="" -p no:cacheprovider -k "prompt_cache or rewrite or supersede or compaction or state_briefing or context_budget or closing_look or render_target or render_check or visual_check_looks_at_a_standalone_svg or visual_budget_stop" tests/test_engine_kernel.py
   ```
   Result: **`10 passed, 111 deselected in 14.75s` (exit code `0`)**.

4. **Static & Hygiene Gates:**
   - `python scripts/check_tree.py`: **`check_tree: ok · 162 modules · 87467 lines · no legacy surface`** (exit code `0`; `533` lines below the `88,000` ceiling).
   - `python -m ruff check src tests scripts --no-cache`: **`All checks passed!`** (exit code `0`).
   - `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui src/karox/web_check.py src/karox/visual_check.py`: **`Success: no issues found in 15 source files`** (exit code `0`).
   - `git diff --check`, `git diff 623851b..HEAD --check`, and `git diff a9235cc..HEAD --check`: **all exit `0`**.

---

## 5. Safety Counters & Outstanding Live Validation

- `real_browser_runs`: `0`
- `provider_calls`: `0`
- `network_calls`: `0`
- `paid_calls`: `0`
- `other_worktrees_changed`: `0`
- `full_pytest`: `NOT_RUN` (prohibited by offline browser-free boundary)
- `live_browser_conformance`: `NOT_RUN`
- `live_three_arm_benchmark`: `NOT_RUN`

### Outstanding Real-Browser & Performance Unknowns

- All validation in Mission 14 and Mission 14.1 is strictly offline using deterministic fake Playwright views and fake providers.
- Real headless Chromium behavior on Windows (ANGLE D3D11) and Linux (SwiftShader), actual wall-time reductions on static SVG (`pelican`) and 3D WebGL (`aquarium`, `3d-game`), and live provider vision token savings remain `NOT_RUN` until explicitly approved by the project owner.

---

## 6. Decision

**Decision: `ACCEPT CANDIDATE`**
