# KaroX 5 checker-origin repair — 2026-11-08

**Branch:** `goal/gpt6-sol-triad-20261108` (worktree only, not the user's main branch).

## Reproducible root cause
The `karox.web_check` self-integrity routine lives in `web_document.py` but compared its own `__file__` (`web_document.py`) against `importlib.util.find_spec("karox.web_check").origin` (`web_check.py`). Two genuine modules therefore looked like a shadow attempt. This appeared during the Three.js Crystal Cavern DEV run: the agent's `checks.run` returned an integrity rejection while independent real Chromium/WebGL grading passed 4/4. The CLI ended `verification_failed` (exit 1), rather than declaring success.

## Small repair
In `src/karox/web_document.py`, compare the path of the actual `web_check.py` sibling. Reject a foreign untrusted module origin when the executed module lies inside the task repository. For a genuine checker installed outside the writable repository, a second installed KaroX location is not evidence of repository shadowing. The separate checker challenge/anti-forgery protocol is untouched. No test, browser, agent, MCP, CLI, or permission check is disabled.

## Direct evidence
- A fresh RED regression failed 1/3 tests on the original source (expected false positive).
- With the repaired source, **84 targeted tests passed** in 2.47 seconds: `tests/test_web_checker_origin.py`, `tests/test_web_check.py`, `tests/test_completion_gate.py`.
- Direct CLI check from the strict Three.js fixture returned exit 0 and `{"ok":true,"problems":[],"checked":["index.html"],"checked_by":"karox.web_check"}`.
- A locally built candidate wheel contains the correction: `dist-triad-candidate/karox_runtime-5.0.0-py3-none-any.whl`, SHA-256 `485c8ebfbcff994941c09375eda7907c29ad99fd74bedb542b448c2b0affbdc0`. Candidate has **not** yet been through an independent paid 3-way rerun.
- Earlier Three.js independent screenshot adjudication exists in `THREE_D_POSTHOC_ADJUDICATION.json`; never alter the original raw result files.

## Remaining limitations
This repair corrects one real verifier defect; it **does not** prove every KaroX finish gate on all WebGL or visual tasks will succeed, nor imply a competitive win. Rebuilding the wheel alone is not a full product regression. The independent DEV/holdout arena should continue to require grader pass, valid visual evidence, CLI completion, immutable source hashes and explicit memory/disk guards.
