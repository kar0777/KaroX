# MISSION 13 — KV-CACHE PRESERVATION AUDIT REPORT

- **Date:** 2026-11-06
- **Repository:** `D:\проекты\KaroX-v5`
- **Worktree:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win`
- **Starting HEAD:** `c892e3f01b190e50b7f715168b6dcdd8ec0437e4`
- **Starting Branch:** `audit/dsh-offline-test-closure-20261106`
- **Mission Branch:** `mission-13/kv-cache-preservation-20261106`

---

## Phase 1. Characterization of Context Budgets, Reserves, and History Mutations

### 1. Why `_engine_context_budget()` equals `312,000` on a `1,000,000` context window
In `src/karox/engine_kernel.py` (`EngineKernel._engine_context_budget()`):
1. For `effort="ultra"` (`src/karox/effort.py`), `ContextBudget.utilization = 0.8`. With `--context-window 1000000` (`max_input_tokens = 1_000_000`), `ContextBudget.token_ceiling = int(1_000_000 * 0.8) = 800_000`.
2. Completion output tokens share the same model context window. Subtracting `--max-output-tokens 384000` (`reserve = 384_000`) from `token_ceiling` leaves `800_000 - 384_000 = 416_000` in product token units (`chars / 3.0`).
3. Because `DefaultAgent._estimate_tokens()` (`src/karox/engine/agents/default.py`) estimates prompt tokens as `chars // 4` whereas `ContextBudget` budgets at `chars / 3.0`, `_engine_context_budget()` scales by `3 / 4` (`0.75`):
   $$\lfloor 416{,}000 \times 0.75 \rfloor = 312{,}000$$
   In character terms, `312,000 * 4 = 1,248,000` characters, which at `chars / 3.0` is `416,000` tokens. Together with the `384,000` output token reserve (`416,000 + 384,000 = 800,000`), this leaves a `200,000`-token (20%) safety buffer below `1,000,000` for tool schemas, chat framing, and BPE tokenizer variance on code/JSON.

### 2. Why `token_ceiling` equals `800,000`
In `src/karox/agent_contract.py` (`ContextBudget.token_ceiling`):
```python
window = self.max_input_tokens or self.fallback_input_tokens
return max(1_000, int(window * float(self.utilization)))
```
For `effort="ultra"`, `utilization = 0.8`, yielding `int(1_000_000 * 0.8) = 800_000` (and `600,000` at default `medium` `utilization = 0.6`). The 20%–40% reserve (`1.0 - utilization`) protects against BPE token density spikes (`2.5–3.2` chars/token on dense code/diffs/JSON), tool schema definitions (`_tools`), and single-turn growth before the next `manage_context()` check.

### 3. What real constraints stood behind `96,000` (`REWRITE_PRESSURE_TOKENS`)
Two distinct constraints motivated `REWRITE_PRESSURE_TOKENS = 96_000` in `src/karox/engine_provider_bridge.py`:
1. **Fallback / 120K–128K context window safety:** When a model's context window is unknown (`fallback_input_tokens = 120_000`) or `128,000`, `120,000 * 0.8 = 96,000` is the entire `token_ceiling`. Above `96,000` tokens, a 128K-window model with a 16K–32K output reserve is near context overflow or hard middle-message dropping.
2. **Cache-break amortization on medium runs:** On a prefix-caching route where cached input tokens cost ~15% of uncached tokens (`$0.03/M` vs `$0.20/M`), rewriting an early message invalidates the cached suffix after that message and re-bills it at full uncached price. Below `~96K` tokens, runs rarely have enough remaining steps for the 15% cached-token savings to repay a full suffix re-bill.
- **Why `96,000` was premature on a `1,000,000` window:** `EngineKernel.run()` passed `rewrite_pressure_tokens=min(REWRITE_PRESSURE_TOKENS, max(8_000, self._engine_context_budget() // 2))`, clamping the threshold at `96,000` even when `_engine_context_budget()` was `312,000` on a `1M` window. As observed in `r3` (`3d-game`, 55 steps, peaks at `96.7K` and `96.8K`), runs that never exceeded `100K–150K` tokens suffered early-history rewrites at `96.7K` and `96.8K`, invalidating the KV cache when >`500K` tokens of usable input window remained.

### 4. Could raising the threshold to `234,000` cause context overflow?
- **Yes, if set as an unconditional constant or without accounting for window size, `utilization`, and `max_output_tokens`:**
  1. On **unknown (`120K` fallback)** or **`128K`** windows (`_engine_context_budget = 27K–64.8K`), `234K` exceeds the entire model context window.
  2. On **`256K`** windows (`_engine_context_budget = 76.8K–129.6K`), `234K` exceeds `256K - 32K = 224K` usable input tokens and would trigger hard middle-message dropping (`DefaultAgent.manage_context()`) or provider HTTP 400 overflow long before `234K`.
  3. Even on a **`1M` window**, if `utilization = 0.6` (`medium`) and `max_output_tokens = 384,000`, `token_ceiling = 600,000`, `usable_ceiling = 216,000`, and `_engine_context_budget = 162,000`. A hardcoded `234,000` constant would exceed `162,000`, causing destructive `DefaultAgent.manage_context()` (which drops middle messages and mutates `messages[1]` with the state briefing) to fire at `162K` before the surgical in-place rewrite ever ran.
- **When `234,000` IS safe (derived from reserves, not hardcoded):**
  On a known `1M` window with `utilization = 0.8` (`token_ceiling = 800,000`) and `max_output_tokens = 384,000`:
  - `usable_ceiling = 800,000 - 384,000 = 416,000` product tokens (`184,000` below `1M - 384K = 616K`),
  - `usable_budget = int(416,000 * 0.75) = 312,000` engine tokens,
  - `headroom = max(24,000, usable_budget // 4) = 78,000` tokens reserved for system prompt, tool schemas, BPE variance, and new turn messages before `_engine_context_budget()` (`312,000`),
  - `rewrite_pressure_tokens = 312,000 - 78,000 = 234,000`.
  Even if real BPE `prompt_tokens` are `1.5x` the `chars // 4` heuristic (`234,000 * 1.5 = 351,000`), `351,000 + 384,000 (output reserve) = 735,000 < 1,000,000`, leaving `265,000` BPE tokens of safety margin against context overflow.

### 5. Which mechanisms independently mutated previously sent messages
Four mechanisms in the baseline (`c892e3f`) could independently mutate previously sent messages:
1. **`EngineAgent._supersede_stale_reads()` (`src/karox/engine_kernel.py`):**
   Mutated `tool_message["content"]` in-place to `_clip_text(content, 1200, _STALE_READ_MARKER)` when a file read was followed by `repo_edit_file`. It evaluated `history_rewrite_allowed(reclaimable_reads)` using **only** stale-read characters, ignored `repo_write_file` and `repo_apply_patch` as file modifications, and did not check whether the edit call succeeded (`<returncode>0</returncode>`).
2. **`EngineProviderModel._request_messages()` (`src/karox/engine_provider_bridge.py`):**
   Mutated `ToolCall.raw_arguments` on the wire via `self._elided_calls` for completed `repo_write_file` / `repo_apply_patch` calls (`> 2,000` chars) older than the last 2 (`_ELIDED_ARGUMENT_KEEP = 2`). It evaluated `history_rewrite_allowed(reclaimable_writes)` **separately** after `manage_context()` had already run, and `DefaultAgent._estimate_tokens()` continued counting the un-elided `raw_arguments` in `self.messages` (risking false compaction).
3. **`DefaultAgent.manage_context()` (`src/karox/engine/agents/default.py`):**
   Triggered when `_estimate_tokens() > context_token_budget`: clipped middle observations to 400 chars, elided all tool arguments `> 400` chars (including `repo_edit_file`), dropped middle messages if still over budget, and clipped tail messages if needed.
4. **`EngineAgent._refresh_state_briefing()` (`src/karox/engine_kernel.py`):**
   Triggered after `DefaultAgent.manage_context()` incremented `n_context_events`: mutated `self.messages[1]["content"]` (the initial user task message at index 1), invalidating the entire conversation prefix after `system`.

### 6. Which changes preserve correct `resume` and `continue`
1. **Durable `record.provider_history` immutability:** `_persist_engine_assistant()` and `_persist_engine_tool_result()` write the assistant tool calls and tool observations to `record.provider_history` when each step executes, before any later in-memory rewrite. Completion gate (`completion_gate.py`, `session_analysis.py`, `requirement_coverage.py`) inspects `record.provider_history` and `record.evidence`, never `agent.messages`.
2. **Valid JSON tool arguments on the wire:** `_elided_argument_marker()` emits valid JSON (`{"_karo_context_manager": "..."}`), satisfying strict OpenAI/Anthropic/DeepSeek schema validation across turns and `agent.continue_run()` repair loops.
3. **Idempotent state briefing on `resume` / `continue`:** `_refresh_state_briefing()` strips any existing `\n\n[KaroX durable state (kept across context compaction)]` suffix before capturing `_briefing_base` and only mutates `messages[_briefing_target]["content"]` when the rendered string actually changes, preventing duplicate state briefing blocks across repeated compactions, `resume`, or `store.continue_task()`.

---

## Phase 2. Minimal Implementation Summary

Files modified (`2` runtime modules, `1` test module):
1. **`src/karox/engine_provider_bridge.py`:**
   - Added `EngineProviderModel.pending_elidable_writes(messages)`, `elide_calls(call_ids)`, and `note_history_rewritten(reclaimed_chars)`.
   - Extended `EngineProviderModel.history_rewrite_allowed(reclaimable_chars, *, estimated_tokens=0)` to evaluate `max(self.last_prompt_tokens, int(estimated_tokens)) >= self.rewrite_pressure_tokens` while preserving full backward compatibility for direct `EngineProviderModel` callers.
2. **`src/karox/engine_kernel.py`:**
   - Added `EngineKernel._rewrite_pressure_tokens()` deriving the pressure threshold from `ContextBudget` (`window_known`, `max_input_tokens`, `token_ceiling`), `max_output_tokens` reserve (defaulting to 20% of `window` when unset on large windows), and explicit headroom (`max(24_000, usable_budget // 4)`) below `_engine_context_budget()`. Preserved `min(REWRITE_PRESSURE_TOKENS, max(8_000, budget // 2))` fallback for unknown or `<= 128K` windows.
   - Unified `_supersede_stale_reads()` and `_elided_calls` into a single coordinated pass inside `EngineAgent._supersede_stale_reads(force=over_budget)`:
     - Expanded `_STALE_EDIT_TOOLS` to `{"repo_edit_file", "repo_write_file", "repo_apply_patch"}` via `_touched_edit_paths()`, ignoring failed edit calls (`returncode != 0`).
     - Combined `reclaimable_reads + reclaimable_writes` into `total_reclaimable` so both mechanisms share one batch gate and execute on the exact same turn instead of causing two separate KV-cache breaks.
   - Overrode `EngineAgent._estimate_tokens()` to subtract elided write argument characters (`self.model._elided_calls`) so `DefaultAgent.manage_context()` measures the actual wire footprint and does not trigger false middle-message compaction.
   - Made `EngineAgent._refresh_state_briefing()` idempotent across repeated compactions and resumed transcripts.

---

## Phase 3 & Phase 4. Regression Tests and Offline Validation

All 15 required scenarios are covered in `tests/test_prompt_cache.py` alongside the existing suite:
1. **Prompt below 96K (`60K`):** `test_prompt_thresholds_below_96k_at_96k_150k_and_234k_on_1m_window` (Request 1: no rewrite).
2. **Prompt around 96K (`97K`):** `test_prompt_thresholds_below_96k_at_96k_150k_and_234k_on_1m_window` (Request 2: no rewrite on 1M window).
3. **Prompt around 150K (`150K`):** `test_prompt_thresholds_below_96k_at_96k_150k_and_234k_on_1m_window` (Request 3: no rewrite on 1M window).
4. **Prompt around 234K (`235K`):** `test_prompt_thresholds_below_96k_at_96k_150k_and_234k_on_1m_window` (Request 4: single coordinated batch rewrite of both stale reads and old writes).
5. **Context window 128K:** `test_context_window_budgets_and_rewrite_pressure_across_windows` + `test_coordinated_batch_combines_stale_reads_and_old_writes` (`engine_budget=64,800`, `pressure=32,400`).
6. **Context window 256K:** `test_context_window_budgets_and_rewrite_pressure_across_windows` (`engine_budget=129,600`, `pressure=97,200`).
7. **Context window 1M:** `test_context_window_budgets_and_rewrite_pressure_across_windows` (`engine_budget=312,000`, `pressure=234,000` at `ultra` + `384K` reserve; `engine_budget=162,000`, `pressure=121,500` at `medium` + `384K` reserve).
8. **Unknown context window:** `test_context_window_budgets_and_rewrite_pressure_across_windows` (`window_known=False`, `engine_budget=54,000`, `pressure=27,000 <= 96,000`).
9. **Repeated history compaction:** `test_repeated_compaction_resume_and_continue_preserve_valid_history` (multiple compactions, single state briefing block).
10. **Resume after compaction:** `test_repeated_compaction_resume_and_continue_preserve_valid_history` (`report2.verified is True`).
11. **Continue after compaction:** `test_repeated_compaction_resume_and_continue_preserve_valid_history` (`store.continue_task` + `report3.verified is True`).
12. **Large tool observations:** `test_large_observations_read_and_write_outputs_bounded_at_creation` (`git.diff` bounded by `output_char_limit` at creation time).
13. **Large `repo_read_file` outputs:** `test_large_observations_read_and_write_outputs_bounded_at_creation` (bounded at creation time with head + tail preserved).
14. **Large `repo_write_file` outputs:** `test_large_observations_read_and_write_outputs_bounded_at_creation` (compact `<800`-char observation at creation time instead of 48KB echo).
15. **Preserving latest file state semantics:** `test_latest_file_state_semantics_preserved_across_read_edit_write_patch` (failed edits never mark earlier reads stale; reads after the last edit stay intact; `repo_edit_file` anchors and last 2 large writes stay intact).

### Offline Validation Results (Browser-Free, Zero Live Provider Calls)
- `python -m pytest tests/test_prompt_cache.py -q` -> **18 passed**
- `python -m pytest tests/test_completion_gate.py tests/test_repo_command_safety.py tests/test_check_probes.py tests/test_layout_recheck.py -q` -> **107 passed, 1 skipped**
- `python -m pytest tests/test_engine_port_context.py tests/test_engine_tool_env_render.py -q` -> **15 passed**
- `python -m pytest tests/test_engine_kernel.py -k "reuse or identical_checks or probe_that_writes or closing_look or visual_budget or web_finish or stale or caching_provider or compaction" -q` -> **27 passed**
- `python -m ruff check src/karox/engine_kernel.py src/karox/engine_provider_bridge.py tests/test_prompt_cache.py` -> **All checks passed!**

---

## Phase 5. Economics & Before/After Comparison

### A. Measured Offline Synthetic Trajectory (6-Step 1M Window Run: `60K -> 97K -> 150K -> 235K -> 180K -> 181K`)

| Metric | Before (`c892e3f`) [Measured Offline] | After (Mission 13) [Measured Offline] | Delta |
| :--- | :--- | :--- | :--- |
| **Rewrite pressure threshold (1M window, `ultra`, 384K output reserve)** | `96,000` tokens | `234,000` tokens | `+138,000` tokens (derived from `312K - 78K` headroom) |
| **Number of history rewrite turns (`history_rewrite` cache breaks)** | **2 separate turns** (Step 3 at `97K` for stale read `c1`; Step 4 at `150K` for old write `c2`) | **1 coordinated turn** (Step 5 after `235K`: `c1` + `c2` rewritten together) | **-1 full prefix-cache invalidation (-50%)** |
| **Rewrite at `97K` (`< 234K`)** | **Yes** (`c1` clipped early, breaking cache at 97K) | **No** (prefix 100% preserved) | Eliminated premature rewrite at ~96K |
| **Rewrite at `150K` (`< 234K`)** | **Yes** (`c2` elided separately, breaking cache a second time at 150K) | **No** (prefix 100% preserved) | Eliminated second rewrite at ~150K |
| **Mutated historical messages across the run** | `2` messages mutated across `2` separate turns | `2` messages (`c1` tool output + `c2` write args) mutated in `1` turn at `>= 234K` | Same `53,949` chars reclaimed, **1 batch instead of 2** |
| **Wire characters removed when pressure (`>= 234K`) is reached** | `26,881` chars (Step 3) + `27,068` chars (Step 4) = `53,949` chars | `53,949` chars (`~13,487` tokens) in a single pass on Step 5 | Coordinated single-pass reclamation |
| **`_estimate_tokens()` overcount after write elision** | `+6,767` tokens overcounted (`_elided_calls` masked wire only, not `_estimate_tokens()`) | **`0` overcount** (`EngineAgent._estimate_tokens()` accounts for `_elided_calls`) | Prevents false `DefaultAgent.manage_context()` compaction |
| **Context overflow risk (128K / 256K / 1M / Unknown)** | Safe on 128K/Unknown (`<= 96K`); over-aggressive on 1M | **Safe on all windows** (`27K` unknown, `32.4K` on 128K, `97.2K` on 256K, `121.5K–234K` on 1M) | `>= 78K` engine tokens (`>= 265K` BPE tokens) headroom on 1M |

### B. Recorded Benchmark Trajectory Projection (`3d-game`, `r3`, 55 Steps on DeepSeek V4.1 Flash)
*Note: The `r3` column contains measured facts from `lab/arena/runs/r3/3d-game/karo/result.json`. The Post-Mission-13 column is an analytical projection of replay behavior on that trajectory (not a live provider run).*

| Metric | Recorded `r3` (`c892e3f` Baseline) [Measured Fact] | Post-Mission-13 Trajectory [Analytical Projection] | Projected Impact |
| :--- | :--- | :--- | :--- |
| **Peak prompt size vs `234K` threshold** | `96,818` tokens (Step 38) | `~155,000` tokens (Step 55, without early rewrites) | Remains strictly below `234,000` (`< 25%` of `1M` window) |
| **History rewrite events (`history_rewrite` full/partial cache breaks)** | **2 events** (Step 34: `96,743 -> 85,849`; Step 39: `96,818 -> 38,996`) | **0 events** (prompt never reaches `234,000` tokens) | **-2 mid-run KV-cache invalidations** |
| **Compaction events (`DefaultAgent.manage_context`)** | `0` | `0` | `0` |
| **Uncached prompt tokens billed ($0.20 / 1M)** | `434,548` tokens (`$0.0869`) | `~185,000–215,000` tokens (`$0.0370–$0.0430`) | **~220K–250K fewer uncached input tokens** (no suffix re-prefill at Steps 34 & 39) |
| **Cached prompt tokens billed ($0.03 / 1M)** | `3,355,664` tokens (`$0.1007`) | `~4,650,000` tokens (`$0.1395`) | Higher cached prefix retention across Steps 34–55 |
| **Net input token cost on `3d-game` ($0.20 uncached / $0.03 cached)** | `$0.1876` | `~$0.1765–$0.1825` | **Net cost reduction (`~$0.005–$0.011`) + eliminates 2 full suffix re-prefill latency stalls** |
| **Context overflow risk on `3d-game`** | `0%` (`96.8K / 1,000K`) | `0%` (`~155K / 1,000K`, `> 460K` tokens below `1M - 384K`) | Zero overflow risk |

---

## Correction Addendum (2026-11-07 — Mission 13.1 Independent Audit Reconciliation)

The table in **Phase 5.B** above preserved above for historical transparency contained an attribution error identified during the Mission 13 Independent Audit:

1. **Two Distinct `3d-game` Runs Were Conflated in Phase 5.B:**
   - **`wandb-3arm-20261103-r3 / 3d-game / karo`** (`arena/runs/wandb-3arm-20261103-r3/3d-game/karo/record.json`, commit `3064208`): **48 steps**, peak `prompt_tokens = 140,417` (Step 45), **1** `history_rewrite` event at **Step 46** (`140,417 -> 134,942`, `cached_tokens: 134,912 -> 0`), total `prompt_tokens = 3,867,091`, `cached_tokens = 3,587,328`, `uncached_tokens = 279,763`, measured input cost **`$0.16357`**.
   - **`m1017-r1 / 3d-game / karo_final`** (`docs/benchmarks/sbx-1017-20261104.md`, commit `e006e71`): **66 steps**, **2** `history_rewrite` events at **Step 34** (`96.7K -> 85.8K`, cache prefix matched `35.6K`) and **Step 39** (`96.8K -> 39.0K`, cache prefix matched `11.9K`), plus **5** `provider_miss` events, total tokens `4.00M`, `uncached_tokens = 459K`.
2. **Full Primary-Evidence Reconciliation and Mission 13.1 Cache-Safety Corrections:**
   - See `docs/audits/MISSION_13_1_CACHE_SAFETY_20261107.md` for the complete reconciliation of `r3` vs `m1017-r1`, the exact Mission 13 commit chain (`c892e3f -> 747a2f2 -> 085abe5 -> b639101`), the P0 state-briefing prefix-preservation fix, the P0 context-window safety guard, and the P1 suffix-aware early high-reclaimability rewrite policy.

