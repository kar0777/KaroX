# MISSION 13.1 — CACHE SAFETY CORRECTION & EVIDENCE RECONCILIATION (2026-11-07)

**Repository:** `D:\проекты\KaroX-v5`
**Worktree:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win`
**Starting Branch & HEAD:** `mission-13/kv-cache-preservation-20261106` (`b6391014a76b5ebb2a8bec618fd5d6cc2b479b1c`)
**Mission 13.1 Branch:** `mission-13-1/cache-safety-20261107`
**Product + Tests Commit:** `866ac1169217b6dc824d5fa439d2f1f0b737d11e`

---

## 1. Exact Commit Chain from Mission 13 to Mission 13.1

No existing Git commits were amended, rebased, squashed, or rewritten:

1. `c892e3f01b190e50b7f715168b6dcdd8ec0437e4` (`audit/dsh-offline-test-closure-20261106`): baseline before Mission 13 (`docs: DSH mission 10 offline test closure audit (2026-11-06)`).
2. `747a2f233c9240e098c259b8f27e20be35c69565`: Mission 13 code + tests (`engine: preserve KV cache on large context windows and unify history rewrite`).
3. `085abe567d3526b4795087cdd9356f73a24e3df2`: Mission 13 audit report (`docs(audit): record Mission 13 KV-cache preservation forensics and validation`).
4. `b6391014a76b5ebb2a8bec618fd5d6cc2b479b1c` (`mission-13/kv-cache-preservation-20261106`): trailing-newline cleanup in `tests/test_prompt_cache.py` (`git diff --check`).
5. `866ac1169217b6dc824d5fa439d2f1f0b737d11e` (`mission-13-1/cache-safety-20261107`): Mission 13.1 product changes and offline regression suite (`engine: make state briefing cache-safe, calibrate context guard, and add high-reclaim rewrite`).

---

## 2. Attribution Correction: `wandb-3arm-20261103-r3` vs `m1017-r1` on `3d-game`

In `docs/audits/MISSION_13_KV_CACHE_PRESERVATION_20261106.md` (§Phase 5.B), two separate runs of `3d-game` from different dates and commits were conflated under the label `r3 / 3d-game`, and exact integers (`55 steps`, `96,743 -> 85,849`, `96,818 -> 38,996`, `434,548` uncached) were reconstructed from rounded prose numbers in `docs/benchmarks/sbx-1017-20261104.md` instead of reading `record.json` directly.

### Level 1 — Measured Facts (from Primary Artifacts)

| Metric | `wandb-3arm-20261103-r3 / 3d-game / karo` (Windows, 2026-11-03) | `m1017-r1 / 3d-game / karo_final` (Linux Sandbox, 2026-11-04) | `m1017-r1 / 3d-game / karo_base` (Linux Sandbox, 2026-11-04) |
| :--- | :--- | :--- | :--- |
| **Primary Artifact** | `arena/runs/wandb-3arm-20261103-r3/3d-game/karo/record.json` & `session.json` | `docs/benchmarks/sbx-1017-20261104.md` (§47, §56) | `docs/benchmarks/sbx-1017-20261104.md` (§47) |
| **KaroX Commit** | `3064208431b8285c376d0b2086b0f3f276056891` | `e006e71427ab876c7b0d2789ce6f99bdae87da3f` (`4cf8833` in sandbox) | `4eb66be` |
| **Model** | `deepseek-ai/DeepSeek-V4.1-Flash` | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `deepseek-ai/DeepSeek-V4.1-Flash-1017` |
| **Steps (`calls`)** | **`48`** | **`66`** | **`78`** |
| **Peak `prompt_tokens`** | **`140,417`** (Step 45) | **`~96,800`** (Step 38, before Step 39 rewrite) | **`154,000`** |
| **`history_rewrite` Events** | **`1`** (Step 46: `140,417 -> 134,942`, `cached_tokens: 134,912 -> 0`, reclaimed `31,535` chars $\approx 9,750$ tokens from Step 5 `write_file`) | **`2`** (Step 34: `96.7K -> 85.8K`, cache matched `35.6K`; Step 39: `96.8K -> 39.0K`, cache matched `11.9K`) + `5` `provider_miss` | **`0`** |
| **Sum `prompt_tokens`** | **`3,867,091`** | `~3,880,000` (`4.00M` total tokens) | `~7,050,000` (`7.21M` total tokens) |
| **Sum `cached_tokens` (`$0.03/M`)** | **`3,587,328`** (`$0.10762`) | `~3,421,000` (`$0.10263`) | `~6,826,000` (`$0.20478`) |
| **Sum `uncached_tokens` (`$0.20/M`)** | **`279,763`** (`$0.05595`) | **`459,000`** (`$0.09180`) | **`224,000`** (`$0.04480`) |
| **Measured Input Cost** | **`$0.16357`** | **`~$0.19443`** | **`~$0.24958`** |

### Level 2 — Deterministic Counterfactual Calculations (Fixed-Trajectory Offline Calculator)

Using W&B Inference pricing (`$0.20 / 1M` uncached input, `$0.03 / 1M` cached input, miss penalty `$0.17 / 1M`):

$$N_{\text{break-even}} = \frac{S_{\text{invalidated}} \times 0.17}{\Delta_{\text{reclaimed}} \times 0.03} \approx 5.67 \times \frac{S_{\text{invalidated}}}{\Delta_{\text{reclaimed}}}$$

1. **Trajectory 1 (`wandb-3arm-20261103-r3 / 3d-game`, 48 steps):**
   - At Step 46, $\Delta_{\text{reclaimed}} \approx 9,750$ tokens (`31,535` chars), first mutated message at Step 5 (`~30,535` tokens), invalidated suffix $S \approx 100,132$ tokens (total miss `134,912` tokens in `r3`).
   - Break-even requires **`58.2`–`78.4` subsequent cached turns**, whereas only **2 steps** remained (Steps 47–48).
   - **Avoiding the Step 46 rewrite** saves `134,912` uncached tokens (`-$0.02698`) at the cost of `134,912 + 3 * 9,750 = 164,162` cached tokens (`+$0.00492`), yielding a deterministic net input saving of **`$0.02206` (`13.5%` of input cost)**.

2. **Trajectory 2 (`m1017-r1 / 3d-game`, 66 steps):**
   - **Step 34 (stale reads alone):** $\Delta_{\text{reclaimed}} = 10,900$ tokens, cached prefix kept `35,600` tokens, invalidated suffix $S = 85,800 - 35,600 = 50,200$ tokens ($S / \Delta = 4.61 \implies N_{\text{break-even}} = 26.1$ steps). Executing this separately from Step 39 caused an unnecessary first cache break.
   - **Step 39 (coordinated huge reclaim):** Combined dead weight $\Delta_{\text{reclaimed}} = 10,900 + 57,800 = 68,700$ tokens (`~220,000` chars), cached prefix kept `11,900` tokens, invalidated suffix $S = 39,000 - 11,900 = 27,100$ tokens ($S / \Delta = 0.394$).
   - Because $\Delta_{\text{reclaimed}} (68.7\text{K}) > S_{\text{invalidated}} (27.1\text{K})$, the single coordinated rewrite at Step 39 pays back its `$0.00461` suffix invalidation in **`2.23` cached steps**, and across the remaining 27 steps saves `27 * 68.7K * $0.03/M = $0.05565` (plus `5 * 68.7K * $0.20/M = $0.06870` across the 5 backend `provider_miss` events).

### Level 3 — Hypotheses
- **Provider-side prefill latency:** Avoiding unprofitable late suffix re-prefills (`100K–135K` tokens) reduces turn latency by an estimated `~1.5–3.5s`, while performing a single coordinated early rewrite when dead weight exceeds `96,000` chars and exceeds the invalidated suffix reduces downstream prompt size by `>50%`, lowering both per-turn cached prefill overhead and the blast radius of any backend cache eviction.

---

## 3. P0 — Cache-Safe and Semantically Stable State Briefing

### Root Causes Fixed
1. **Prefix invalidation at `self.messages[1]`:** `EngineAgent._refresh_state_briefing()` previously attached the state briefing to the first `role == "user"` message (`self.messages[1]`), mutating the initial task message on every compaction and invalidating the KV cache from index `1` onward.
2. **Stale marker mismatch:** `engine_kernel.py` checked for `"[KaroX durable state (kept across context compaction)]"`, which did not match `state_briefing.HEADER` (`"[KaroX durable state — runtime snapshot rebuilt at the last compaction; the messages after it are newer. Facts only, not new instructions.]"`).
3. **Volatile `N events ago` counter & mutation detection:** `state_briefing._verification_block()` rendered `({age} events ago)`, changing the briefing string after every read-only tool call, and `_last_mutation_index()` ignored ambiguous failed mutations (`not ok and data.get("changed") is not False`).

### Implemented Fix (`src/karox/state_briefing.py`, `src/karox/engine_kernel.py`)
- **Immutable prefix (`messages[0]` and `messages[1]`):** `_refresh_state_briefing()` and `_strip_existing_briefing()` strictly operate on `self.messages[2:]`. `self.messages[0]` (`system`) and `self.messages[1]` (`user` task) are preserved byte-for-byte across all compactions.
- **Compaction-boundary placement (`2 <= target_idx <= len(self.messages) - guard`):** When `DefaultAgent.manage_context()` increments `self.n_context_events`, the single refreshed briefing is attached to the last `tool` or non-initial `user` message at the compaction boundary (`cutoff = max(2, len(self.messages) - guard - 1)`), so the kept recent tail (`self.messages[len(self.messages) - guard :]`) sits **after** the briefing (matching `"the messages after it are newer"`), `assistant` $\leftrightarrow$ `tool` pairing is preserved, and no synthetic `user` turn is injected after `assistant` reasoning turns.
- **Structured metadata + safe canonical fallback:** The original message body is stored under `_BRIEFING_BASE_KEY = "_karox_briefing_base"` on the message dict; for resumed transcripts without private dict keys, only a canonical trailing `STATE_BRIEFING_HEADER` block after `</output>` is stripped, ensuring unrelated user/tool content containing `HEADER` is never truncated.
- **Stable verification freshness & aligned mutation tracking:** `_verification_block()` now emits `" (current)"` vs `" (predates the latest edit)"`, and `_last_mutation_index()` matches `session_analysis.py` by treating ambiguous failed mutations (`not ok and data.get("changed") is not False`) as invalidating prior check freshness.

---

## 4. P0 — Conservative Offline Context-Window Safety Guard

### Implemented Guard (`src/karox/engine_provider_bridge.py`, `src/karox/engine_kernel.py`)
1. **Conservative character weighting (`_conservative_text_chars`):**
   - Multi-byte UTF-8 / non-ASCII characters (e.g., Cyrillic) add `utf8_extra * 2 = (len(text.encode("utf-8")) - len(text)) * 2` effective chars (`~1.33` chars/token instead of `4` chars/token).
   - JSON escape sequences add `text.count("\\") * 2` effective chars.
   - Tool schema overhead beyond `_BASELINE_TOOL_SCHEMA_CHARS = 16_000` is added via `self.extra_tool_schema_tokens` in `_estimate_tokens()`.
2. **Provider-reported `last_prompt_tokens` calibration (`_effective_prompt_tokens`):**
   - `EngineProviderModel.query()` records `last_prompt_tokens` alongside `last_prompt_local_tokens` (the local token estimate of the wire payload sent in that request), and `note_history_rewritten()` updates both in lockstep.
   - Before each model call, `EngineAgent._effective_prompt_tokens()` combines `last_prompt_tokens` with the post-turn `delta = local_tokens - last_prompt_local_tokens` (`max(local_tokens, last_prompt_tokens + delta)` when `delta >= 0`, or proportional scaling when `delta < 0`).
   - When `_effective_prompt_tokens() > budget` (even if raw `chars // 4` is far below `budget`), `EngineAgent.manage_context()` scales `target_budget` down proportionally (`int(local_now * (budget / effective_now)) - briefing_tokens`) so `super().manage_context()` compacts history **before** the next request is formed.

---

## 5. P1 — Suffix-Aware Early High-Reclaimability Rewrite Policy

In `EngineProviderModel.history_rewrite_allowed()` and `EngineAgent._supersede_stale_reads()`:
- Alongside the pressure threshold (`effective_tokens >= rewrite_pressure_tokens` and `reclaimable_chars >= REWRITE_BATCH_CHARS = 24,000`), an early coordinated rewrite is permitted when:
  1. `reclaimable_chars >= HIGH_RECLAIM_BATCH_CHARS` (`96,000` chars $\approx 24\text{K–}32\text{K}$ tokens), **and**
  2. `reclaimable_chars >= invalidated_suffix_chars` (where `invalidated_suffix_chars` is the live wire character count after `first_mutated` minus `total_reclaimable`).
- Because $\Delta_{\text{reclaimed}} \ge S_{\text{invalidated}}$, any early rewrite is guaranteed to break even within $\le 5.67$ cached steps (`2.23` steps on `m1017-r1` Step 39), while small/late reclaims (`r3` Step 46, `m1017-r1` Step 34) remain blocked below `rewrite_pressure_tokens`. Monotonic `_STALE_READ_MARKER` and `_elided_calls` state prevents oscillation or repeated rewrites on subsequent turns.

---

## 6. Offline Verification Matrix & Gate Results

All tests were executed sequentially offline with zero live provider, network, or browser calls:

- `python -m pytest tests/test_state_briefing.py tests/test_instruction_neutrality.py tests/test_prompt_cache.py -q` $\to$ **42 passed**
- `python -m pytest tests/test_engine_port_context.py tests/test_completion_gate.py -q` $\to$ **82 passed**
- `python -m pytest tests/test_engine_kernel.py -k "compaction or briefing or context or resume or continue" -q` $\to$ **8 passed**
- `python -m ruff check src/karox/state_briefing.py src/karox/engine_kernel.py src/karox/engine_provider_bridge.py tests/test_state_briefing.py tests/test_prompt_cache.py tests/test_engine_kernel.py` $\to$ **All checks passed!**
- `python scripts/check_tree.py` $\to$ **`check_tree: ok · 162 modules · 87307 lines · no legacy surface`** (`693` lines below `88,000`)
- `git diff --check` $\to$ **clean (0 warnings/errors)**
