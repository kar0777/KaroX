# Phase 0: Semantic Trace Profiler

Status: immediate next implementation candidate after the current v7 campaign.

The current AgentKernel already records extensive economy metrics. Phase 0 should extend and reorganize them rather than build a second accounting stack.

## 1. Existing useful metrics

Current `agent.py` already records, per response/session, among other fields:

- prompt/completion token usage;
- cache read/write tokens;
- tool schema bytes advertised/unique;
- cache prefix stability;
- context tier/window information;
- selected/omitted tool families;
- read-cache hits/misses;
- ContextCompiler item counts and chars in/out;
- stale/reused chars;
- superseded full-write payload chars;
- batched turns avoided;
- continuity-compaction stats;
- provider/model/cost metadata.

Phase 0 should preserve these names and add a higher-level semantic profile.

## 2. New purpose of the profiler

Answer four questions for every run:

1. Why was each model call made?
2. What information did the call consume?
3. What durable progress did it produce?
4. Could the same transition have been deterministic?

The profiler must not affect execution decisions.

## 3. Per-turn record

Proposed record:

```json
{
  "step": 12,
  "provider": "...",
  "model": "...",
  "purpose": "repair",
  "trigger": "new_check_failure",
  "input": {
    "tokens": 42000,
    "cached_tokens": 38000,
    "system_chars": 10064,
    "tool_schema_bytes": 12400,
    "message_chars": 130000,
    "context_chars_in": 125000,
    "context_chars_out": 91000,
    "source_task_chars": null,
    "tool_result_chars": null,
    "assistant_history_chars": null
  },
  "output": {
    "tokens": 8000,
    "tool_calls": 1,
    "mutation_payload_chars": 11000,
    "visible_text_chars": 250,
    "reasoning_tokens": null
  },
  "effects": {
    "repository_changed": true,
    "changed_paths": ["src/x.py"],
    "new_evidence": 0,
    "new_passing_check": false,
    "new_failure": false,
    "plan_changed": false,
    "progress_class": "provisional_mutation"
  },
  "waste_flags": []
}
```

Fields unavailable from current runtime should be null, not estimated into fake precision.

## 4. Turn purpose vocabulary

Use a small stable taxonomy:

- `explore`: model chooses/requests repository or external information;
- `design`: semantic architecture/API choice;
- `synthesize`: generates first implementation for a task region;
- `diagnose`: reasons about a concrete failure/defect;
- `repair`: generates a fix for an already identified defect;
- `verify`: model decides/runs/interprets checks or inspection mainly to establish correctness;
- `visual_judge`: semantic visual evaluation;
- `report`: primarily user-facing summary;
- `recovery`: provider/tool/protocol recovery;
- `no_progress`: call produced no new useful state.

This is an analysis label, not a runtime role.

## 5. Classification method

Do not add a second LLM call to classify model calls.

Use deterministic event features plus optional offline manual correction.

Examples:

### Explore

- only read/search/fetch/list/inspect calls;
- no mutation;
- not immediately driven by a concrete failure.

### Synthesize

- first mutation in a task/region without an active failure.

### Repair

- mutation follows an active concrete failure/defect.

### Verify

- checks/status/diff/read after mutations with no new mutation proposal.

### Report

- no tools, final-style content, and task state already has current verification evidence.

### No progress

- no repository/evidence/task-state novelty;
- repeats earlier tool/action or narration;
- not a justified user-facing report.

The raw features must remain available so incorrect classification does not corrupt evidence.

## 6. Progress event model

Track objective state changes between model turns.

Possible novelty:

- repository content hash changed;
- changed scope expanded/shrank;
- new check/evidence record;
- failure localized/resolved;
- new user gate;
- new durable decision;
- new source-span coverage (future Semantic Executive);
- process state advanced.

A model call that emits thousands of tokens but changes none of these is a strong optimization candidate.

## 7. Waste flags

Suggested flags:

- `repeated_read_unchanged`;
- `repeated_check_same_revision`;
- `tool_schema_error`;
- `failed_exact_edit_anchor`;
- `superseded_generated_payload`;
- `verification_narration`;
- `repeated_final_narration`;
- `same_interrupt_equivalent_state`;
- `external_fetch_not_found`;
- `plan_bookkeeping_only`;
- `no_state_progress`.

These are descriptive. They do not automatically imply the call was unnecessary.

## 8. Context decomposition

Current metrics show total/context compiler chars but not necessarily the semantic source of every byte.

Add best-effort accounting at request assembly:

- system;
- tool schemas;
- original user task/project instructions;
- assistant text history;
- tool-result history;
- tool-call argument history;
- compaction/continuity material;
- images metadata separately;
- request-only dynamic notes.

The byte categories must sum to the actual serialized model-facing representation as closely as practical, with an `unclassified_chars` remainder rather than forced attribution.

## 9. Generated-code survival

Track model-generated mutation payloads by content hash.

At end of run classify payload bytes:

- survive in final artifact;
- partially survive;
- superseded by later write;
- failed to apply;
- reverted/discarded.

Key metric:

```
surviving_generated_code_bytes / total_generated_mutation_bytes
```

This is especially important for greenfield tasks where output tokens are inherently large.

## 10. Run-level report

`semantic_profile.json` should include:

### Totals

- model calls;
- calls by purpose;
- model input/output/cached tokens;
- wall time;
- tool calls;
- failed tools/edits;
- generated mutation bytes;
- surviving mutation bytes;
- checks/evidence;
- final outcome.

### Opportunity estimate

Without claiming counterfactual token savings, report counts such as:

- verification-only model calls;
- no-progress calls;
- low-level schema/tool errors;
- repeated unchanged reads;
- repeated same-revision checks;
- final/report turns after current verification already existed.

Call this `deterministic_transition_candidates`, not "calls that would definitely be eliminated".

## 11. Aquarium v7 campaign seed observations

The completed three-run campaign gives several concrete patterns the profiler must make visible.

### Run 1

- 32 model steps;
- 37 tool calls;
- 1 failed edit;
- repeated web fetches including 404s;
- incorrect `repo.search` arguments once;
- multiple repeated reads of the generated HTML;
- repeated final checks/narration;
- large rewritten/superseded file drafts;
- external arena pass despite model-reported remaining semantic defect.

### Run 2

- 27 model steps, 33 tool calls;
- 1,026,457 input / 897,024 cached / 205,473 output tokens;
- Karo session ended `verified/completed`;
- external grader failed because the browser shader did not compile: GLSL identifier `half` was reserved.

The profiler must therefore distinguish `internal_verification_complete` from `external_task_outcome` rather than collapsing both into success.

### Run 3

- 41 model steps, 53 tool calls;
- 2,055,239 input / 1,897,216 cached / 187,670 output tokens;
- Karo session ended `verified/completed`;
- external grader failed because `scaleBias` was undefined at page load and the render was essentially one colour.

### Cross-run signals

- same-task input varied from about 1.03M to 2.06M tokens;
- steps varied 27-41;
- tool calls varied 33-53;
- output remained very large in every run (~188k-210k);
- every run recorded one failed edit;
- grade was only 1/3 despite two runs reaching Karo's internal completed/verified state;
- most input was provider-reported cache reads, so profiler economics must separate cached and uncached traffic.

If the profiler cannot make these differences and internal-vs-external outcome mismatch visible in structured data, it is not useful enough.

## 12. Implementation placement

Prefer:

- one small profiler module consuming existing run/session events;
- minimal additional counters in `AgentKernel` only where required;
- an offline/report generator over durable session/usage data.

Avoid invasive changes to provider execution before the baseline is captured.

## 13. Acceptance for Phase 0

Before Semantic Executive code starts, the profiler should answer from a run artifact:

- which 5 turns cost the most input/output;
- which turns were verification/reporting vs synthesis/repair;
- how much request material was system/tools/history/tool results;
- how much generated mutation content survived;
- how many calls produced no durable progress;
- where exact edit/tool schema failures happened.

The purpose is diagnosis, not optimization yet.


## 14. Current Phase 0 implementation

The first non-interfering implementation now lives in `src/karox/semantic_shadow.py`.

In addition to the original per-boundary labels, the run summary reports:

- cost by effect kind;
- the five costliest model boundaries by uncached prompt + completion tokens;
- boundaries with tool errors;
- repeated unchanged read results, keyed by hashed call/result signatures;
- repeated same-state proof results;
- `deterministic_transition_candidates` with reason counts and the observed
  prompt/cache/completion token cost attached to those boundaries.

The candidate token totals are deliberately **not** reported as counterfactual
savings. They are the measured cost of boundaries worth investigating.

For offline inspection of a durable run, use:

```bash
python scripts/semantic_shadow_report.py path/to/session.json
python scripts/semantic_shadow_report.py --json path/to/session.json
```

The reporter accepts a full session record (with `usage`), raw usage JSON, or
an agent report containing `project_context.semantic_shadow`.

Harbor's Karo adapter also exposes an opt-in `semantic_shadow` boolean. It is
false by default and exists only to make diagnostic benchmark runs reproducible.

### Safety finding from the first optimization experiment

A verified proof chain is **not** sufficient evidence that the next provider
turn is disposable. A regression test demonstrates a valid case where the next
turn launches an additional check that fails and therefore invalidates the
previously green state. An experimental runtime shortcut that ended immediately
after verification was reverted after this test failed.

That negative result is part of the profiler's purpose: a boundary may be a
high-value optimization candidate without being safe to remove generically.
Future runtime elimination should therefore require a stronger state contract
than `verification_complete` alone.
